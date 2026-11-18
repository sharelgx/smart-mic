#include "mic_wire.h"
#include "esp_random.h"
#include "mbedtls/md.h"
#include "lwip/sockets.h"
#include <string.h>

static int transfer(int fd, void *data, size_t size, int writing) {
    uint8_t *p=data;
    while(size) {
        int n=writing ? send(fd,p,size,0) : recv(fd,p,size,0);
        if(n<=0) return -1;
        p+=n; size-=(size_t)n;
    }
    return 0;
}
static int mac(const uint8_t key[32], const void *data, size_t n, uint8_t out[32]) {
    return mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256),key,32,data,n,out);
}
static void header_make(uint8_t h[12], uint64_t sequence, uint32_t size) {
    for(int i=7;i>=0;i--) { h[i]=(uint8_t)sequence; sequence>>=8; }
    for(int i=11;i>=8;i--) { h[i]=(uint8_t)size; size>>=8; }
}
int mic_wire_open(mic_wire_t *w, int fd, const uint8_t key[32]) {
    memset(w,0,sizeof(*w)); w->fd=fd;
    mbedtls_gcm_init(&w->up); mbedtls_gcm_init(&w->down);
    uint8_t challenge[20], reply[48], msg[64], derived[32];
    if(transfer(fd,challenge,20,0) || memcmp(challenge,"PVM1",4)) return -1;
    esp_fill_random(reply,16);
    memcpy(msg,challenge,20); memcpy(msg+20,reply,16);
    if(mac(key,msg,36,reply+16) || transfer(fd,reply,48,1)) return -1;
    const char *labels[]={"passport-up","passport-down"};
    mbedtls_gcm_context *contexts[]={&w->up,&w->down};
    for(int i=0;i<2;i++) {
        size_t n=strlen(labels[i]);
        memcpy(msg,labels[i],n); memcpy(msg+n,challenge+4,16); memcpy(msg+n+16,reply,16);
        if(mac(key,msg,n+32,derived) || mbedtls_gcm_setkey(contexts[i],MBEDTLS_CIPHER_ID_AES,derived,256)) return -1;
    }
    memset(derived,0,sizeof(derived));
    return 0;
}
int mic_wire_send(mic_wire_t *w, uint8_t kind, const void *data, size_t n) {
    uint8_t packet[12+1024+17], plain[1025], iv[12]={0};
    if(n>1024) return -1;
    plain[0]=kind; if(n) memcpy(plain+1,data,n);
    header_make(packet,w->tx,(uint32_t)n+17); memcpy(iv+4,packet,8);
    if(mbedtls_gcm_crypt_and_tag(&w->up,MBEDTLS_GCM_ENCRYPT,n+1,iv,12,packet,12,plain,packet+12,16,packet+13+n)) return -1;
    if(transfer(w->fd,packet,n+29,1)) return -1;
    w->tx++; return 0;
}
int mic_wire_read(mic_wire_t *w, uint8_t *kind, void *data, size_t capacity) {
    uint8_t h[12], expected[12], body[1041], plain[1025], iv[12]={0};
    if(transfer(w->fd,h,12,0)) return -1;
    uint32_t n=0; for(int i=8;i<12;i++) n=(n<<8)|h[i];
    header_make(expected,w->rx,n);
    if(memcmp(expected,h,8) || n<17 || n>sizeof(body) || n-17>capacity) return -1;
    if(transfer(w->fd,body,n,0)) return -1;
    memcpy(iv+4,h,8);
    if(mbedtls_gcm_auth_decrypt(&w->down,n-16,iv,12,h,12,body+n-16,16,body,plain)) return -1;
    *kind=plain[0]; memcpy(data,plain+1,n-17); w->rx++; return (int)n-17;
}
void mic_wire_free(mic_wire_t *w) {
    mbedtls_gcm_free(&w->up); mbedtls_gcm_free(&w->down);
}
