// Passport Wi-Fi microphone. Board drivers stay in the upstream BSP.
#include "bsp_audio.h"
#include "bsp_button.h"
#include "bsp_battery.h"
#include "bsp_display.h"
#include "mic_wire.h"
#include "mic_pixel.h"
#include "app_pages.h"
#include "lvgl.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_system.h"
#include "driver/usb_serial_jtag.h"
#include "cJSON.h"
#include "mbedtls/sha256.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "lwip/sockets.h"
#include <stdatomic.h>
#include <fcntl.h>
#include <errno.h>
#include <unistd.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

LV_FONT_DECLARE(passport_ui_16);
LV_FONT_DECLARE(passport_ui_24);

#define GREEN 0x72F1A1
#define BG 0x080E0B
#define RATE 16000
#define CHUNK 320
#define MAX_CHUNKS (60*RATE/CHUNK)
#define WIFI_UP BIT0
#define CONFIGURED BIT1

typedef struct { uint8_t kind; uint16_t size; uint8_t data[CHUNK*2]; } audio_frame_t;
typedef struct { bsp_btn_t button; bsp_btn_ev_t event; } key_event_t;
static QueueHandle_t audio_queue, keys, page_keys;
static atomic_int active_page = PAGE_MENU;
static atomic_bool voice_busy;
static EventGroupHandle_t flags;
static TaskHandle_t capture_task_handle;
static atomic_bool capturing, cancel_recording, capture_done, capture_failed, connected;
static atomic_int meter;
static atomic_int wifi_reason;
static atomic_bool wifi_scanning;
static atomic_uint key_events, key_presses[3], network_loops, ui_loops;
static atomic_int last_key=-1, last_key_event=-1;
static uint8_t pair_key[32];
static char host[16];
static int port;
static atomic_bool config_ready;
static lv_obj_t *status_label,*detail_label,*battery_label,*battery_icon,*time_label;
static int battery_level=-1;
static int battery_pulse=-1;
static lv_obj_t *left_wave,*right_wave;
static lv_obj_t *chat_panel,*chat_counter,*chat_title,*chat_bitmap_obj,*chat_hint;
#define CHAT_BITMAP_WIDTH 200
#define CHAT_BITMAP_HEIGHT 72
#define CHAT_BITMAP_BYTES (CHAT_BITMAP_WIDTH * CHAT_BITMAP_HEIGHT / 8)
static uint8_t chat_bitmap[CHAT_BITMAP_BYTES], selected_bitmap[CHAT_BITMAP_BYTES];
#define PROJECT_BITMAP_HEIGHT 20
#define PROJECT_BITMAP_BYTES (CHAT_BITMAP_WIDTH * PROJECT_BITMAP_HEIGHT / 8)
static uint8_t chat_project_pixels[PROJECT_BITMAP_BYTES], selected_project_pixels[PROJECT_BITMAP_BYTES];
static lv_obj_t *selected_title,*chat_project,*selected_project;
static const char *chat_controls="上下键浏览\n第三键选择，长按移除";
#define CONVERSATION_MIC_SCALE 4
static int wave_level = -1;  // -1 is idle; 0 is recording in silence.
static const char *TAG="wifi_mic";
static atomic_uint capture_started;

static void screen_status(const char *status, const char *detail) {
    if(!bsp_lvgl_lock(100)) return;
    lv_label_set_text(status_label,status); lv_label_set_text(detail_label,detail);
    bsp_lvgl_unlock();
}
static lv_obj_t *label(lv_obj_t *root, const char *text, int x, int y, int width, uint32_t color) {
    lv_obj_t *o=lv_label_create(root); lv_label_set_text(o,text);
    lv_obj_set_pos(o,x,y); lv_obj_set_width(o,width);
    lv_obj_set_style_text_font(o,&passport_ui_16,0);
    lv_obj_set_style_text_color(o,lv_color_hex(color),0);
    return o;
}
static void pixel_rect(lv_layer_t *layer, lv_draw_rect_dsc_t *ink, int x, int y,
                       int width, int height, uint32_t color) {
    lv_area_t area = {x, y, x + width - 1, y + height - 1};
    ink->bg_color = lv_color_hex(color);
    lv_draw_rect(layer, ink, &area);
}

static void draw_microphone(lv_event_t *event) {
    lv_area_t box;
    lv_obj_get_coords(lv_event_get_target_obj(event), &box);
    lv_layer_t *layer = lv_event_get_layer(event);
    lv_draw_rect_dsc_t ink;
    lv_draw_rect_dsc_init(&ink);
    for (int y = 0; y < MIC_PIXEL_HEIGHT; y++) {
        const char *row = mic_pixel_rows[y];
        for (int x = 0; x < MIC_PIXEL_WIDTH;) {
            int start = x++;
            while (x < MIC_PIXEL_WIDTH && row[x] == row[start]) x++;
            if (row[start] != '.') {
                pixel_rect(layer, &ink, box.x1 + start * CONVERSATION_MIC_SCALE, box.y1 + y * CONVERSATION_MIC_SCALE,
                           (x - start) * CONVERSATION_MIC_SCALE, CONVERSATION_MIC_SCALE, mic_pixel_palette[row[start] - '1']);
            }
        }
    }
}

static void draw_wave(lv_event_t *event) {
    lv_obj_t *wave = lv_event_get_target_obj(event);
    lv_area_t box;
    lv_obj_get_coords(wave, &box);
    lv_draw_rect_dsc_t ink;
    lv_draw_rect_dsc_init(&ink);
    int center_y = box.y1 + 36;
    for (int col = 0; col < 4; col++) {
        int distance = wave == left_wave ? 3 - col : col;
        int height = wave_level - distance;
        if (height < 0) height = 0;
        for (int row = -height; row <= height; row++) {
            uint32_t color = wave_level < 0 ? 0x284A37 : (distance < 2 ? GREEN : 0x419963);
            pixel_rect(lv_event_get_layer(event), &ink, box.x1 + col * 8, center_y + row * 6,
                       5, 4, color);
        }
    }
}

static lv_obj_t *pixel_art(lv_obj_t *parent, int x, int y, int width, int height, lv_event_cb_t draw) {
    lv_obj_t *obj = lv_obj_create(parent);
    lv_obj_remove_style_all(obj);
    lv_obj_remove_flag(obj, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(obj, width, height);
    lv_obj_set_pos(obj, x, y);
    lv_obj_add_event_cb(obj, draw, LV_EVENT_DRAW_MAIN, NULL);
    return obj;
}

static void update_wave(bool recording, int peak_percent) {
    // A doubling in peak level lights the next pair of blocks (about 6 dB).
    int level = -1;
    if (recording) {
        level = 0;
        for (int threshold = 2; threshold <= 64 && peak_percent >= threshold; threshold *= 2) level++;
    }
    if (level != wave_level) {
        wave_level = level;
        lv_obj_invalidate(left_wave);
        lv_obj_invalidate(right_wave);
    }
}

static void draw_chat_bitmap(lv_event_t *event) {
    lv_obj_t *target=lv_event_get_target_obj(event);
    bool project=target==chat_project || target==selected_project;
    const uint8_t *pixels=chat_bitmap;
    if(target==selected_title) pixels=selected_bitmap;
    if(target==chat_project) pixels=chat_project_pixels;
    if(target==selected_project) pixels=selected_project_pixels;
    lv_area_t box;
    lv_obj_get_coords(lv_event_get_target_obj(event),&box);
    lv_layer_t *layer=lv_event_get_layer(event);
    lv_draw_rect_dsc_t ink;
    lv_draw_rect_dsc_init(&ink);
    ink.bg_color=lv_color_hex(0xF0F7F2);
    bool compact=target==selected_title;
    int height=project?PROJECT_BITMAP_HEIGHT:(compact?CHAT_BITMAP_HEIGHT/4:CHAT_BITMAP_HEIGHT);
    for(int y=0;y<height;y++) {
        int start=-1;
        for(int x=0;x<=CHAT_BITMAP_WIDTH;x++) {
            bool on=false;
            if(x<CHAT_BITMAP_WIDTH) {
                if(compact) {
                    // Each half of the single line holds one original row at half scale.
                    int source_x=(x%(CHAT_BITMAP_WIDTH/2))*2;
                    int source_y=y*2+(x>=CHAT_BITMAP_WIDTH/2?CHAT_BITMAP_HEIGHT/2:0);
                    int offset=source_y*(CHAT_BITMAP_WIDTH/8)+source_x/8;
                    uint8_t mask=0xC0u>>(source_x%8);
                    on=(pixels[offset]|pixels[offset+CHAT_BITMAP_WIDTH/8]) & mask;
                } else {
                    on=pixels[y*(CHAT_BITMAP_WIDTH/8)+x/8] & (0x80u>>(x%8));
                }
            }
            if(on && start<0) start=x;
            if(!on && start>=0) {
                pixel_rect(layer,&ink,box.x1+start,box.y1+y,x-start,1,project?0xACBBB1:0xF0F7F2);
                start=-1;
            }
        }
    }
}

static void draw_battery(lv_event_t *event) {
    lv_area_t box;
    lv_obj_get_coords(lv_event_get_target_obj(event),&box);
    lv_layer_t *layer=lv_event_get_layer(event);
    lv_draw_rect_dsc_t ink;
    lv_draw_rect_dsc_init(&ink);
    int x=box.x1, y=box.y1;
    const uint32_t edge=0xACBBB1;
    pixel_rect(layer,&ink,x,y,16,1,edge);
    pixel_rect(layer,&ink,x,y+8,16,1,edge);
    pixel_rect(layer,&ink,x,y,1,9,edge);
    pixel_rect(layer,&ink,x+15,y,1,9,edge);
    pixel_rect(layer,&ink,x+17,y+3,2,3,edge);
    int fill=(battery_level*12+99)/100;
    if(battery_level>0) pixel_rect(layer,&ink,x+2,y+2,fill,5,
                                   battery_level<=20?0xFF7166:GREEN);
    if(battery_pulse>=0) {
        int pulse_x=battery_pulse*2;
        pixel_rect(layer,&ink,x+2+pulse_x,y+2,2,5,0xE0FFF2);
    }
}

static lv_obj_t *screen_init(void) {
    lv_obj_t *scr=lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr,lv_color_hex(BG),0);
    lv_obj_remove_flag(scr,LV_OBJ_FLAG_SCROLLABLE);
    label(scr,"Codex 语音麦克风",18,20,160,GREEN);
    battery_icon=pixel_art(scr,173,24,19,9,draw_battery);
    battery_label=label(scr,"--%",192,20,45,0xACBBB1);
    lv_obj_set_style_text_font(battery_label,&lv_font_montserrat_14,0);
    lv_obj_set_style_text_align(battery_label,LV_TEXT_ALIGN_RIGHT,0);
    selected_title=pixel_art(scr,18,63,CHAT_BITMAP_WIDTH,CHAT_BITMAP_HEIGHT/4,draw_chat_bitmap);
    lv_obj_add_flag(selected_title,LV_OBJ_FLAG_HIDDEN);
    selected_project=pixel_art(scr,18,41,CHAT_BITMAP_WIDTH,PROJECT_BITMAP_HEIGHT,draw_chat_bitmap);
    lv_obj_add_flag(selected_project,LV_OBJ_FLAG_HIDDEN);
    pixel_art(scr,78,85,MIC_PIXEL_WIDTH*CONVERSATION_MIC_SCALE,
              MIC_PIXEL_HEIGHT*CONVERSATION_MIC_SCALE,draw_microphone);
    left_wave=pixel_art(scr,26,103,29,76,draw_wave);
    right_wave=pixel_art(scr,185,103,29,76,draw_wave);
    status_label=label(scr,"等待配网",18,194,204,0xF0F7F2);
    lv_obj_set_style_text_font(status_label,&passport_ui_24,0);
    lv_obj_set_style_text_align(status_label,LV_TEXT_ALIGN_CENTER,0);
    time_label=label(scr,"00:00 / 01:00",18,234,204,0xACBBB1);
    lv_obj_set_style_text_align(time_label,LV_TEXT_ALIGN_CENTER,0);
    detail_label=label(scr,"请用 USB 线连接电脑",18,272,204,0xACBBB1);
    lv_obj_set_style_text_align(detail_label,LV_TEXT_ALIGN_CENTER,0);
    chat_panel=lv_obj_create(scr);
    lv_obj_set_size(chat_panel,240,320);
    lv_obj_set_pos(chat_panel,0,0);
    lv_obj_set_style_bg_color(chat_panel,lv_color_hex(BG),0);
    lv_obj_set_style_border_width(chat_panel,0,0);
    lv_obj_set_style_pad_all(chat_panel,0,0);
    lv_obj_remove_flag(chat_panel,LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(chat_panel,LV_OBJ_FLAG_HIDDEN);
    lv_obj_t *heading=label(chat_panel,"聊天列表",18,25,204,GREEN);
    chat_counter=label(chat_panel,"",18,68,204,0xACBBB1);
    chat_title=label(chat_panel,"",18,116,204,0xF0F7F2);
    chat_bitmap_obj=pixel_art(chat_panel,18,116,CHAT_BITMAP_WIDTH,CHAT_BITMAP_HEIGHT,draw_chat_bitmap);
    lv_obj_add_flag(chat_bitmap_obj,LV_OBJ_FLAG_HIDDEN);
    chat_project=pixel_art(chat_panel,18,90,CHAT_BITMAP_WIDTH,PROJECT_BITMAP_HEIGHT,draw_chat_bitmap);
    lv_obj_add_flag(chat_project,LV_OBJ_FLAG_HIDDEN);
    chat_hint=label(chat_panel,chat_controls,18,240,204,0xACBBB1);
    lv_obj_set_style_text_font(heading,&passport_ui_24,0);
    lv_obj_set_style_text_font(chat_counter,&lv_font_montserrat_14,0);

    lv_obj_t *back=label(scr,"长按上键返回",18,297,204,0xACBBB1);
    lv_obj_set_style_text_align(back,LV_TEXT_ALIGN_CENTER,0);
    lv_obj_move_foreground(chat_panel);
    return scr;
}
static void chat_screen(const char *counter,const char *title) {
    if(!bsp_lvgl_lock(100)) return;
    if(!counter) lv_obj_add_flag(chat_panel,LV_OBJ_FLAG_HIDDEN);
    else {
        lv_label_set_text(chat_counter,counter);
        lv_label_set_text(chat_title,title);
        lv_obj_remove_flag(chat_title,LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(chat_bitmap_obj,LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(chat_project,LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(chat_panel,LV_OBJ_FLAG_HIDDEN);
    }
    bsp_lvgl_unlock();
}

static void show_project(lv_obj_t *object,uint8_t *buffer,const uint8_t *pixels) {
    if(pixels) {
        memcpy(buffer,pixels,PROJECT_BITMAP_BYTES);
        lv_obj_remove_flag(object,LV_OBJ_FLAG_HIDDEN);
        lv_obj_invalidate(object);
    } else {
        lv_obj_add_flag(object,LV_OBJ_FLAG_HIDDEN);
    }
}

static void chat_screen_bitmap(const char *counter,const uint8_t *pixels,const uint8_t *project) {
    if(!bsp_lvgl_lock(100)) return;
    memcpy(chat_bitmap,pixels,sizeof(chat_bitmap));
    show_project(chat_project,chat_project_pixels,project);
    lv_label_set_text(chat_counter,counter);
    lv_obj_add_flag(chat_title,LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(chat_bitmap_obj,LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(chat_panel,LV_OBJ_FLAG_HIDDEN);
    lv_obj_invalidate(chat_bitmap_obj);
    bsp_lvgl_unlock();
}

static void conversation_title(const uint8_t *pixels,const uint8_t *project) {
    if(!bsp_lvgl_lock(100)) return;
    show_project(selected_project,selected_project_pixels,project);
    if(pixels) {
        memcpy(selected_bitmap,pixels,sizeof(selected_bitmap));
        lv_obj_remove_flag(selected_title,LV_OBJ_FLAG_HIDDEN);
        lv_obj_invalidate(selected_title);
    } else {
        lv_obj_add_flag(selected_title,LV_OBJ_FLAG_HIDDEN);
    }
    bsp_lvgl_unlock();
}

static void chat_remove_hint(bool confirm) {
    if(!bsp_lvgl_lock(100)) return;
    lv_label_set_text(chat_hint,confirm?"从设备移除这个聊天？\n第三键确认，上键取消":chat_controls);
    bsp_lvgl_unlock();
}

static void key_callback(bsp_btn_t button,bsp_btn_ev_t event,void *user) {
    (void)user;
    atomic_fetch_add(&key_events,1);
    atomic_store(&last_key,button); atomic_store(&last_key_event,event);
    if(event==BSP_BTN_PRESS && button>=BSP_BTN_UP && button<=BSP_BTN_OK)
        atomic_fetch_add(&key_presses[button],1);
    key_event_t key={button,event};
    if(atomic_load(&active_page)!=PAGE_VOICE ||
       (button==BSP_BTN_UP && event==BSP_BTN_LONG)) {
        if(event==BSP_BTN_CLICK || event==BSP_BTN_LONG) xQueueSend(page_keys,&key,0);
        return;
    }
    if(button==BSP_BTN_UP) {
        if(event!=BSP_BTN_CLICK) return;
        key.event=BSP_BTN_PRESS; // A short press browses; a hold belongs to the launcher.
    } else if(event!=BSP_BTN_PRESS && !(button==BSP_BTN_OK &&
             (event==BSP_BTN_LONG || event==BSP_BTN_RELEASE))) return;
    xQueueSend(keys,&key,0);
}
static void radio_event(void *arg,esp_event_base_t base,int32_t id,void *data) {
    (void)arg;
    if(base==WIFI_EVENT && id==WIFI_EVENT_STA_START) esp_wifi_connect();
    if(base==WIFI_EVENT && id==WIFI_EVENT_STA_DISCONNECTED) {
        atomic_store(&wifi_reason,((wifi_event_sta_disconnected_t *)data)->reason);
        xEventGroupClearBits(flags,WIFI_UP);
        if(!atomic_load(&wifi_scanning)) esp_wifi_connect();
    }
    if(base==IP_EVENT && id==IP_EVENT_STA_GOT_IP) xEventGroupSetBits(flags,WIFI_UP);
}
static bool field(cJSON *json,const char *name,const char **out,size_t max) {
    cJSON *p=cJSON_GetObjectItemCaseSensitive(json,name);
    if(!cJSON_IsString(p) || strlen(p->valuestring)>max) return false;
    *out=p->valuestring; return true;
}
static int hex_value(char c) {
    if(c>='0' && c<='9') return c-'0';
    if(c>='a' && c<='f') return c-'a'+10;
    return -1;
}
static bool configure(char *line) {
    const char *prefix="@PVM1 CONFIG ";
    if(strncmp(line,prefix,strlen(prefix))) return false;
    if(atomic_load(&capturing)) return false;
    uint8_t new_key[32]={0};
    cJSON *json=cJSON_Parse(line+strlen(prefix));
    const char *ssid=NULL,*password=NULL,*address=NULL,*key=NULL;
    cJSON *p=cJSON_GetObjectItemCaseSensitive(json,"port");
    bool ok=json && field(json,"ssid",&ssid,32) && strlen(ssid)>0 &&
        field(json,"password",&password,63) && field(json,"host",&address,15) &&
        field(json,"key",&key,64) && strlen(key)==64 && cJSON_IsNumber(p) && p->valuedouble==p->valueint && p->valueint>0 && p->valueint<=65535;
    struct in_addr ip;
    if(ok && inet_pton(AF_INET,address,&ip)!=1) ok=false;
    if(ok) for(int i=0;i<32;i++) {
        int a=hex_value(key[i*2]),b=hex_value(key[i*2+1]);
        if(a<0 || b<0) { ok=false; break; }
        new_key[i]=(uint8_t)((a<<4)|b);
    }
    if(ok) {
        wifi_config_t wifi={0};
        memcpy(wifi.sta.ssid,ssid,strlen(ssid)); memcpy(wifi.sta.password,password,strlen(password));
        wifi.sta.pmf_cfg.capable=true;
        if(atomic_load(&config_ready)) {
            atomic_store(&wifi_scanning,true); esp_wifi_disconnect(); vTaskDelay(pdMS_TO_TICKS(200));
        }
        ok=esp_wifi_set_config(WIFI_IF_STA,&wifi)==ESP_OK;
        memset(&wifi,0,sizeof(wifi));
        if(ok) {
            nvs_handle_t store;
            ok=nvs_open("passport_mic",NVS_READWRITE,&store)==ESP_OK;
            if(ok) {
                ok=nvs_set_blob(store,"pair_key",new_key,32)==ESP_OK &&
                    nvs_set_str(store,"host",address)==ESP_OK &&
                    nvs_set_u16(store,"port",(uint16_t)p->valueint)==ESP_OK &&
                    nvs_set_u8(store,"version",1)==ESP_OK && nvs_commit(store)==ESP_OK;
                nvs_close(store);
            }
            if(ok) { printf("@PVM1 CONFIGURED\n"); fflush(stdout); }
        }
    }
    if(password) memset((char *)password,0,strlen(password));
    if(key) memset((char *)key,0,strlen(key));
    cJSON_Delete(json); memset(new_key,0,sizeof(new_key));
    if(ok) {
        // One clean owner reloads new network/key settings; no concurrent rekey.
        vTaskDelay(pdMS_TO_TICKS(250)); esp_restart();
    }
    if(atomic_load(&config_ready)) {
        atomic_store(&wifi_scanning,false); esp_wifi_connect();
    }
    return ok;
}
static void update_host(char *line) {
    const char *prefix="@PVM1 HOST ";
    if(strncmp(line,prefix,strlen(prefix)) || atomic_load(&capturing)) return;
    cJSON *json=cJSON_Parse(line+strlen(prefix));
    const char *address=NULL; struct in_addr ip;
    cJSON *p=cJSON_GetObjectItemCaseSensitive(json,"port");
    bool ok=json && field(json,"host",&address,15) && inet_pton(AF_INET,address,&ip)==1 &&
        cJSON_IsNumber(p) && p->valuedouble==p->valueint && p->valueint>0 && p->valueint<=65535;
    nvs_handle_t store;
    if(ok && nvs_open("passport_mic",NVS_READWRITE,&store)==ESP_OK) {
        uint8_t version=0;
        ok=nvs_get_u8(store,"version",&version)==ESP_OK && version==1 &&
            nvs_set_str(store,"host",address)==ESP_OK &&
            nvs_set_u16(store,"port",(uint16_t)p->valueint)==ESP_OK && nvs_commit(store)==ESP_OK;
        nvs_close(store);
        if(ok) { printf("@PVM1 HOST_UPDATED\n"); fflush(stdout); }
    } else ok=false;
    cJSON_Delete(json);
    if(ok) { vTaskDelay(pdMS_TO_TICKS(250)); esp_restart(); }
}
static void report_status(void) {
    wifi_ap_record_t ap;
    esp_err_t associated=esp_wifi_sta_get_ap_info(&ap);
    esp_netif_ip_info_t ip={0};
    esp_netif_t *netif=esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if(netif) esp_netif_get_ip_info(netif,&ip);
    printf("@PVM1 STATUS configured=%d associated=%d wifi_up=%d reason=%d ip=" IPSTR " mac_connected=%d\n",
        atomic_load(&config_ready),associated==ESP_OK,
        (xEventGroupGetBits(flags)&WIFI_UP)!=0,atomic_load(&wifi_reason),
        IP2STR(&ip.ip),atomic_load(&connected));
    printf("@PVM1 KEYS events=%u up=%u down=%u ok=%u last_key=%d last_event=%d mv=%d pending=%u capture=%d done=%d failed=%d net_loops=%u ui_loops=%u\n",
        atomic_load(&key_events),atomic_load(&key_presses[0]),atomic_load(&key_presses[1]),atomic_load(&key_presses[2]),
        atomic_load(&last_key),atomic_load(&last_key_event),bsp_button_read_mv(),(unsigned)uxQueueMessagesWaiting(keys),
        atomic_load(&capturing),atomic_load(&capture_done),atomic_load(&capture_failed),
        atomic_load(&network_loops),atomic_load(&ui_loops));
    fflush(stdout);
}
static void check_ssid(char *line, bool change) {
    const char *prefix=change?"@PVM1 SSID ":"@PVM1 SCAN ";
    if(strncmp(line,prefix,strlen(prefix)) || atomic_load(&capturing)) return;
    cJSON *json=cJSON_Parse(line+strlen(prefix)); const char *target=NULL;
    if(!json || !field(json,"ssid",&target,32) || !strlen(target)) { cJSON_Delete(json); return; }
    wifi_config_t saved={0};
    esp_err_t error=esp_wifi_get_config(WIFI_IF_STA,&saved);
    bool matches=error==ESP_OK && strnlen((char *)saved.sta.ssid,32)==strlen(target) &&
        !memcmp(saved.sta.ssid,target,strlen(target));
    if(change) {
        // Stop the ongoing association before changing STA configuration.
        // ESP-IDF rejects set_config while the station is still connecting.
        atomic_store(&wifi_scanning,true);
        esp_wifi_disconnect(); vTaskDelay(pdMS_TO_TICKS(200));
        if(error==ESP_OK) {
            memset(saved.sta.ssid,0,sizeof(saved.sta.ssid));
            memcpy(saved.sta.ssid,target,strlen(target));
            error=esp_wifi_set_config(WIFI_IF_STA,&saved);
        }
        memset(&saved,0,sizeof(saved)); cJSON_Delete(json);
        if(error==ESP_OK) {
            printf("@PVM1 SSID_UPDATED\n"); fflush(stdout);
            vTaskDelay(pdMS_TO_TICKS(250)); esp_restart();
        }
        printf("@PVM1 SSID_ERROR code=%d\n",(int)error); fflush(stdout);
        atomic_store(&wifi_scanning,false); esp_wifi_connect();
        return;
    }
    memset(&saved,0,sizeof(saved));
    atomic_store(&wifi_scanning,true);
    esp_wifi_disconnect(); vTaskDelay(pdMS_TO_TICKS(200));
    wifi_scan_config_t scan={.show_hidden=true,.scan_type=WIFI_SCAN_TYPE_ACTIVE};
    scan.scan_time.active.min=100; scan.scan_time.active.max=200;
    error=esp_wifi_scan_start(&scan,true);
    uint16_t visible=0;
    if(error==ESP_OK) error=esp_wifi_scan_get_ap_num(&visible);
    uint16_t count=visible>64?64:visible;
    wifi_ap_record_t *records=calloc(count?count:1,sizeof(*records));
    if(error==ESP_OK && records) error=esp_wifi_scan_get_ap_records(&count,records);
    else if(!records) error=ESP_ERR_NO_MEM;
    unsigned found=0;
    if(error==ESP_OK) for(int i=0;i<count;i++) if(!strcmp((char *)records[i].ssid,target)) found++;
    printf("@PVM1 SCAN configured_match=%d error=%d visible=%u matches=%u",matches,(int)error,visible,found);
    if(error==ESP_OK) for(int i=0;i<count;i++) if(!strcmp((char *)records[i].ssid,target))
        printf(" channel=%u rssi=%d auth=%d",records[i].primary,records[i].rssi,records[i].authmode);
    printf("\n"); fflush(stdout);
    free(records); cJSON_Delete(json);
    atomic_store(&wifi_scanning,false); esp_wifi_connect();
}
static void usb_config_task(void *arg) {
    (void)arg;
    char line[1024]; size_t used=0; bool discard=false;
    while(true) {
        uint8_t c;
        if(usb_serial_jtag_read_bytes(&c,1,pdMS_TO_TICKS(250))<=0) continue;
        if(c=='\n') {
            line[used]='\0';
            if(!discard) {
                configure(line); update_host(line); check_ssid(line,false); check_ssid(line,true);
                if(!strcmp(line,"@PVM1 STATUS")) report_status();
                else if(!strcmp(line,"@PVM1 BATTERY")) bsp_battery_report();
            }
            memset(line,0,sizeof(line)); used=0; discard=false;
        } else if(used<sizeof(line)-1 && !discard) line[used++]=(char)c;
        else discard=true;
    }
}
static bool enqueue(audio_frame_t *frame) {
    if(xQueueSend(audio_queue,frame,0)==pdTRUE) return true;
    atomic_store(&capture_failed,true); atomic_store(&capturing,false); return false;
}
static void capture_task(void *arg) {
    (void)arg;
    while(true) {
        ulTaskNotifyTake(pdTRUE,portMAX_DELAY);
        audio_frame_t frame={.kind=MIC_START};
        const char *format="{\"rate\":16000,\"bits\":16,\"channels\":1}";
        frame.size=(uint16_t)strlen(format); memcpy(frame.data,format,frame.size);
        mbedtls_sha256_context sha; mbedtls_sha256_init(&sha);
        mbedtls_sha256_starts(&sha,0);
        size_t total=0;
        if(bsp_audio_set_format(RATE,16,1)!=ESP_OK || !enqueue(&frame)) atomic_store(&capture_failed,true);
        while(atomic_load(&capturing) && !atomic_load(&capture_failed) && total<MAX_CHUNKS*CHUNK*2) {
            frame.kind=MIC_AUDIO; frame.size=CHUNK*2;
            if(bsp_audio_read(frame.data,frame.size)!=ESP_OK) { atomic_store(&capture_failed,true); break; }
            int16_t *samples=(int16_t *)frame.data; int32_t peak=0;
            for(int i=0;i<CHUNK;i++) { int32_t v=samples[i]; if(v<0) v=-v; if(v>peak) peak=v; }
            atomic_store(&meter,peak*100/32768);
            mbedtls_sha256_update(&sha,frame.data,frame.size); total+=frame.size;
            if(!enqueue(&frame)) break;
        }
        uint8_t hash[32]; char hex[65]; mbedtls_sha256_finish(&sha,hash); mbedtls_sha256_free(&sha);
        for(int i=0;i<32;i++) snprintf(hex+i*2,3,"%02x",hash[i]);
        frame.kind=(atomic_load(&cancel_recording) || atomic_load(&capture_failed)) ? MIC_CANCEL : MIC_END;
        frame.size=(uint16_t)snprintf((char *)frame.data,sizeof(frame.data),"{\"bytes\":%u,\"sha256\":\"%s\"}",(unsigned)total,hex);
        enqueue(&frame);
        atomic_store(&capturing,false); atomic_store(&capture_done,true);
    }
}
static int tcp_connect(void) {
    int fd=socket(AF_INET,SOCK_STREAM,IPPROTO_TCP);
    if(fd<0) return -1;
    struct timeval timeout={.tv_sec=5};
    setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
    setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
    struct sockaddr_in addr={.sin_family=AF_INET,.sin_port=htons((uint16_t)port)};
    inet_pton(AF_INET,host,&addr.sin_addr);
    // A non-blocking connect bounds shutdown/retry even if the Mac is offline.
    int old=fcntl(fd,F_GETFL,0); fcntl(fd,F_SETFL,old|O_NONBLOCK);
    int result=connect(fd,(struct sockaddr *)&addr,sizeof(addr));
    if(result<0 && errno==EINPROGRESS) {
        fd_set set; FD_ZERO(&set); FD_SET(fd,&set);
        int error=0; socklen_t size=sizeof(error);
        if(select(fd+1,NULL,&set,NULL,&timeout)>0 && getsockopt(fd,SOL_SOCKET,SO_ERROR,&error,&size)==0 && error==0) result=0;
    }
    fcntl(fd,F_SETFL,old);
    if(result<0) { close(fd); return -1; }
    return fd;
}
static bool voice_begin(void) {
    if(!bsp_lvgl_lock(100)) return false;
    bool active=atomic_load(&active_page)==PAGE_VOICE;
    if(active) atomic_store(&voice_busy,true);
    bsp_lvgl_unlock();
    return active;
}

// The recording worker is idle for this entire phase. A cancel closes the
// session so no pending speech frames can spill into the next recording.
static bool play_reply(mic_wire_t *wire) {
    struct timeval timeout={.tv_sec=5};
    setsockopt(wire->fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
    screen_status("已发送","等待回复，下键取消");
    int16_t pcm[512];
    bool playing=false;
    TickType_t started=xTaskGetTickCount();
    while ((xEventGroupGetBits(flags)&WIFI_UP) &&
           xTaskGetTickCount()-started < pdMS_TO_TICKS(780000)) {
        key_event_t key;
        if(xQueueReceive(keys,&key,pdMS_TO_TICKS(10))==pdTRUE && key.button==BSP_BTN_DOWN)
            return false;
        char peek;
        int pending=recv(wire->fd,&peek,1,MSG_PEEK|MSG_DONTWAIT);
        if(pending<0 && (errno==EAGAIN || errno==EWOULDBLOCK)) continue;
        if(pending<=0) return false;

        uint8_t kind=0;
        int size=mic_wire_read(wire,&kind,pcm,sizeof(pcm));
        if(kind==MIC_SPEECH_END && size==0) return true;
        if(size<=0 || size%2 || kind!=MIC_SPEECH) return false;
        if(!playing) {
            if(bsp_audio_set_format(RATE,16,1)!=ESP_OK) return false;
            bsp_audio_set_volume(100);
            screen_status("正在朗读","下键取消");
            playing=true;
        }
        if(bsp_audio_write(pcm,(size_t)size)!=ESP_OK) return false;
    }
    return false;
}

static void network_task(void *arg) {
    (void)arg;
    xEventGroupWaitBits(flags,CONFIGURED,pdFALSE,pdTRUE,portMAX_DELAY);
    while(true) {
        if (!(xEventGroupGetBits(flags) & WIFI_UP))
            screen_status("正在连接无线网","请确认路由器已开启");
        xEventGroupWaitBits(flags,WIFI_UP,pdFALSE,pdTRUE,portMAX_DELAY);
        screen_status("正在连接电脑","请在电脑上启动音频桥");
        int fd=tcp_connect();
        if(fd<0) { vTaskDelay(pdMS_TO_TICKS(2000)); continue; }
        mic_wire_t wire; uint8_t kind; char result[1+3+CHAT_BITMAP_BYTES/2]={0};
        static uint8_t menu_pixels[CHAT_BITMAP_BYTES], project_pixels[PROJECT_BITMAP_BYTES];
        bool has_project=false;
        bool ready=false;
        if(mic_wire_open(&wire,fd,pair_key)==0 && mic_wire_read(&wire,&kind,result,sizeof(result)-1)>=0 && kind==MIC_READY) ready=true;
        bool menu_supported=ready && !strcmp(result,"virtual-mic-menu");
        bool reply_audio=false;
        if(menu_supported) {
            if(mic_wire_send(&wire,MIC_BIND,"reply-audio",sizeof("reply-audio")-1)) ready=false;
            else {
                int n=mic_wire_read(&wire,&kind,result,sizeof(result)-1);
                ready=n>=0 && kind==MIC_RESULT;
                reply_audio=ready && n==3 && !memcmp(result,"yes",3);
            }
        }
        bool menu_open=false, selected=false, has_menu_bitmap=false;
        bool remove_confirm=false, ignore_release=false, menu_ok_held=false;
        unsigned index=0, count=0;
        if(ready) {
            atomic_store(&connected,true);
            screen_status("就绪","按最下方第三键开始说话");
            xQueueReset(keys);
        }
        bool in_record=false;
        while(ready && (xEventGroupGetBits(flags)&WIFI_UP)) {
            atomic_fetch_add(&network_loops,1);
            key_event_t key;
            if(xQueueReceive(keys,&key,pdMS_TO_TICKS(in_record?1:20))==pdTRUE) {
                if(atomic_load(&active_page)!=PAGE_VOICE) continue;
                if(!in_record && menu_supported && (menu_open || key.button==BSP_BTN_UP || (!selected && key.button==BSP_BTN_OK))) {
                    if(menu_open && key.button==BSP_BTN_OK) {
                        if(key.event==BSP_BTN_PRESS) {
                            menu_ok_held=true;
                            continue;
                        }
                        if(!menu_ok_held) continue;
                        if(key.event==BSP_BTN_RELEASE) menu_ok_held=false;
                    }
                    if(menu_open && key.event==BSP_BTN_LONG) {
                        ignore_release=true;
                        if(count) {
                            remove_confirm=true;
                            chat_remove_hint(true);
                        }
                        continue;
                    }
                    if(key.event==BSP_BTN_RELEASE && ignore_release) {
                        ignore_release=false;
                        continue;
                    }
                    if(!menu_open && key.event!=BSP_BTN_PRESS) continue;
                    bool remove=remove_confirm && key.button==BSP_BTN_OK;
                    if(remove_confirm) {
                        remove_confirm=false;
                        chat_remove_hint(false);
                        if(!remove) continue;
                    }
                    uint8_t request[2];
                    const void *payload="open3";
                    size_t size=5;
                    bool select=menu_open && key.button==BSP_BTN_OK && count && !remove;
                    if(menu_open && count) {
                        if(!select && !remove) index=(index+count+(key.button==BSP_BTN_DOWN?1:count-1))%count;
                        request[0]=remove?2:(select?1:0); request[1]=(uint8_t)index;
                        payload=request; size=sizeof(request);
                    } else if(menu_open && key.button==BSP_BTN_OK) {
                        menu_open=false;
                        chat_screen(NULL,NULL);
                        continue;
                    }
                    if(mic_wire_send(&wire,MIC_BIND,payload,size)) break;
                    struct timeval menu_timeout={.tv_sec=25};
                    setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&menu_timeout,sizeof(menu_timeout));
                    int n=mic_wire_read(&wire,&kind,result,sizeof(result)-1);
                    if(n<0 || kind!=MIC_RESULT) break;
                    result[n]='\0';
                    xQueueReset(keys);
                    if(select && !strcmp(result,"bound")) {
                        menu_open=false; selected=true;
                        conversation_title(has_menu_bitmap?menu_pixels:NULL,has_project?project_pixels:NULL);
                        chat_screen(NULL,NULL);
                        screen_status("就绪","按第三键开始说话");
                    } else {
                        menu_open=true;
                        has_menu_bitmap=false;
                        has_project=false;
                        char *name=strchr(result,'\n');
                        unsigned position=0;
                        if(n==3+CHAT_BITMAP_BYTES/2 && (result[0]=='Q' || result[0]=='P') &&
                           (uint8_t)result[1]<(uint8_t)result[2] && (uint8_t)result[2]<=12) {
                            index=(uint8_t)result[1]; count=(uint8_t)result[2];
                            char counter[16];
                            snprintf(counter,sizeof(counter),"%u/%u",index+1,count);
                            has_project=result[0]=='P';
                            memcpy(menu_pixels,result+3,CHAT_BITMAP_BYTES/2);
                            n=mic_wire_read(&wire,&kind,menu_pixels+CHAT_BITMAP_BYTES/2,CHAT_BITMAP_BYTES/2);
                            if(n!=CHAT_BITMAP_BYTES/2 || kind!=MIC_RESULT) break;
                            if(has_project) {
                                n=mic_wire_read(&wire,&kind,project_pixels,sizeof(project_pixels));
                                if(n!=sizeof(project_pixels) || kind!=MIC_RESULT) break;
                            }
                            has_menu_bitmap=true;
                            chat_remove_hint(false);
                            chat_screen_bitmap(counter,menu_pixels,has_project?project_pixels:NULL);
                        } else if(name && sscanf(result,"%u/%u",&position,&count)==2 &&
                           position>0 && position<=count && count<=12) {
                            chat_remove_hint(false);
                            *name='\0'; index=position-1;
                            chat_screen(result,name+1);
                        } else {
                            count=0;
                            chat_screen("",!strcmp(result,"empty")?"请先打开聊天并发送消息":"无法读取聊天");
                            if(bsp_lvgl_lock(100)) {
                                lv_label_set_text(chat_hint,"上键重试，第三键退出");
                                bsp_lvgl_unlock();
                            }
                        }
                    }
                } else if(key.event==BSP_BTN_PRESS && key.button==BSP_BTN_OK && !in_record && voice_begin()) {
                    xQueueReset(audio_queue); atomic_store(&capture_failed,false);
                    atomic_store(&cancel_recording,false); atomic_store(&capture_done,false);
                    atomic_store(&capturing,true); in_record=true; atomic_store(&capture_started,xTaskGetTickCount());
                    screen_status("正在收音","再按第三键结束，下键取消");
                    xTaskNotifyGive(capture_task_handle);
                } else if(key.event==BSP_BTN_PRESS && in_record && (key.button==BSP_BTN_OK || key.button==BSP_BTN_DOWN)) {
                    if(key.button==BSP_BTN_DOWN) atomic_store(&cancel_recording,true);
                    atomic_store(&capturing,false);
                }
            }
            audio_frame_t frame;
            if(in_record && xQueueReceive(audio_queue,&frame,pdMS_TO_TICKS(5))==pdTRUE) {
                const char *selected_format="{\"rate\":16000,\"bits\":16,\"channels\":1,\"selected\":true}";
                const char *reply_format="{\"rate\":16000,\"bits\":16,\"channels\":1,\"replyAudio\":true}";
                const char *selected_reply_format="{\"rate\":16000,\"bits\":16,\"channels\":1,\"selected\":true,\"replyAudio\":true}";
                const char *format=NULL;
                if(frame.kind==MIC_START) {
                    if(reply_audio) format=selected?selected_reply_format:reply_format;
                    else if(selected) format=selected_format;
                }
                const void *payload=format?(const void *)format:frame.data;
                size_t size=format?strlen(format):frame.size;
                if(atomic_load(&capture_failed) || mic_wire_send(&wire,frame.kind,payload,size)) break;
                if(frame.kind==MIC_END || frame.kind==MIC_CANCEL) {
                    screen_status("正在处理","请稍候");
                    struct timeval timeout={.tv_sec=120}; setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
                    int n=mic_wire_read(&wire,&kind,result,sizeof(result)-1);
                    if(n<0 || kind!=MIC_RESULT) break;
                    result[n]='\0'; in_record=false; xQueueReset(keys);
                    if(!strcmp(result,"sent-voice")) {
                        while(!atomic_load(&capture_done)) vTaskDelay(pdMS_TO_TICKS(1)+1);
                        if(!play_reply(&wire)) break;
                        xQueueReset(keys);
                        atomic_store(&voice_busy,false);
                        screen_status("已发送","目标聊天已收到消息");
                        continue;
                    }
                    atomic_store(&voice_busy,false);
                    if(!strcmp(result,"sent")) screen_status("已发送","目标聊天已收到消息");
                    else if(!strcmp(result,"preview")) screen_status("文字预览","请在电脑上查看");
                    else if(!strcmp(result,"checked")) screen_status("收音完成","音频已传到电脑");
                    else if(!strcmp(result,"silent")) screen_status("未听清","请靠近麦克风再试");
                    else screen_status("已取消","按第三键重新开始");
                }
            }
            if(in_record && atomic_load(&capture_failed)) break;
            if(!in_record) {
                // Detect a closed idle connection without sending or resending commands.
                char peek; int n=recv(fd,&peek,1,MSG_PEEK|MSG_DONTWAIT);
                if(n==0 || n>0 || (n<0 && errno!=EAGAIN && errno!=EWOULDBLOCK)) break;
            }
        }
        atomic_store(&connected,false); atomic_store(&cancel_recording,true); atomic_store(&capturing,false);
        while(in_record && !atomic_load(&capture_done)) vTaskDelay(pdMS_TO_TICKS(20));
        shutdown(fd,SHUT_RDWR); close(fd); mic_wire_free(&wire);
        atomic_store(&voice_busy,false);
        chat_remove_hint(false);
        chat_screen(NULL,NULL);
        conversation_title(NULL,NULL);
        screen_status("连接已断开","正在重新连接");
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}
void app_main(void) {
    ESP_ERROR_CHECK(nvs_flash_init()); // Never erase existing NVS as recovery.
    app_pages_load_preferences();
    ESP_ERROR_CHECK(bsp_display_init());
    if(!bsp_lvgl_init()) { ESP_LOGE(TAG,"LVGL init failed"); return; }
    if(!bsp_lvgl_lock(500)) return;
    app_pages_init(screen_init()); bsp_lvgl_unlock(); bsp_display_backlight(60);
    flags=xEventGroupCreate(); audio_queue=xQueueCreate(24,sizeof(audio_frame_t)); keys=xQueueCreate(8,sizeof(key_event_t));
    page_keys=xQueueCreate(8,sizeof(key_event_t));
    if(!flags || !audio_queue || !keys || !page_keys) { screen_status("启动失败","设备内存不足"); return; }
    if(bsp_audio_init()!=ESP_OK) { screen_status("麦克风异常","收音模块启动失败"); return; }
    if(bsp_button_init(key_callback,NULL)!=ESP_OK) { screen_status("按键异常","按键模块启动失败"); return; }
#ifdef SMART_MIC_YOUTELI_BATTERY
    // Opt-in for boards confirmed for the factory Youteli profile.
    bool battery_present=bsp_battery_wake_existing()==ESP_OK;
    if(battery_present) {
        esp_err_t battery_init=bsp_battery_init();
        ESP_LOGI(TAG,"battery initialization: %s",esp_err_to_name(battery_init));
    }
#else
    bool battery_present=bsp_battery_wake_existing()==ESP_OK;
#endif
    ESP_ERROR_CHECK(esp_netif_init()); ESP_ERROR_CHECK(esp_event_loop_create_default());
    if(!esp_netif_create_default_wifi_sta()) return;
    wifi_init_config_t init=WIFI_INIT_CONFIG_DEFAULT(); ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_FLASH));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,radio_event,NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,radio_event,NULL));
    usb_serial_jtag_driver_config_t usb={.tx_buffer_size=512,.rx_buffer_size=2048};
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb));
    if(xTaskCreate(capture_task,"mic_capture",4096,NULL,6,&capture_task_handle)!=pdPASS ||
       xTaskCreate(network_task,"mic_network",8192,NULL,4,NULL)!=pdPASS ||
       xTaskCreate(usb_config_task,"mic_config",4096,NULL,3,NULL)!=pdPASS) {
        screen_status("启动失败","无法启动设备任务"); return;
    }
    nvs_handle_t store;
    if(nvs_open("passport_mic",NVS_READONLY,&store)==ESP_OK) {
        uint8_t version=0; size_t key_size=32,host_size=sizeof(host); uint16_t saved_port=0;
        bool valid=nvs_get_u8(store,"version",&version)==ESP_OK && version==1 &&
            nvs_get_blob(store,"pair_key",pair_key,&key_size)==ESP_OK && key_size==32 &&
            nvs_get_str(store,"host",host,&host_size)==ESP_OK &&
            nvs_get_u16(store,"port",&saved_port)==ESP_OK && saved_port>0;
        nvs_close(store);
        if(valid) {
            port=saved_port; atomic_store(&config_ready,true);
            ESP_ERROR_CHECK(esp_wifi_start()); xEventGroupSetBits(flags,CONFIGURED);
        }
    }
    int64_t last_report=0, next_battery_read=0;
    int battery_soc=-1, displayed_soc=-1;
    int fallback_mv=0, displayed_mv=0;
    int displayed_seconds=0;
    while(true) {
        atomic_fetch_add(&ui_loops,1);
        int64_t now=esp_timer_get_time();
        if(now>=next_battery_read && !atomic_load(&capturing)) {
            if(!battery_present) battery_present=bsp_battery_wake_existing()==ESP_OK;
            battery_soc=battery_present?bsp_battery_existing_soc():-1;
            fallback_mv=(battery_present && battery_soc<0)?bsp_battery_mv():0;
            if(fallback_mv<3000 || fallback_mv>4350) fallback_mv=0;
            next_battery_read=now+10000000;
        }
        if(bsp_lvgl_lock(50)) {
            key_event_t input;
            while(xQueueReceive(page_keys,&input,0)==pdTRUE) {
                app_page_t page=atomic_load(&active_page);
                if(page==PAGE_VOICE && atomic_load(&voice_busy)) continue;
                app_page_t next=app_pages_key(page,input.button,input.event);
                if(page!=next) {
                    xQueueReset(keys);
                    atomic_store(&active_page,next);
                    ESP_LOGI(TAG,"active page=%d",next);
                }
            }
            if(atomic_load(&active_page)==PAGE_TIMER) app_pages_tick(now);
            if(battery_soc!=displayed_soc || fallback_mv!=displayed_mv) {
                if(battery_soc>=0) lv_label_set_text_fmt(battery_label,"%d%%",battery_soc);
                else if(fallback_mv) lv_label_set_text_fmt(battery_label,"%d.%02dV",fallback_mv/1000,(fallback_mv%1000)/10);
                else lv_label_set_text(battery_label,"--%");
                displayed_mv=fallback_mv;
                displayed_soc=battery_soc;
                battery_level=battery_soc;
                if(fallback_mv) {
                    // Voltage-only bar; the numeric label shows the measured volts.
                    battery_level=(fallback_mv-3300)*100/900;
                    if(battery_level<0) battery_level=0;
                    if(battery_level>100) battery_level=100;
                }
                lv_obj_invalidate(battery_icon);
                ESP_LOGI(TAG,"battery_soc=%d",battery_soc);
            }
            int pulse=usb_serial_jtag_is_connected() && battery_soc>=0 && battery_soc<100
                      ? (int)((now/150000)%6) : -1;
            if(pulse!=battery_pulse) {
                battery_pulse=pulse;
                lv_obj_invalidate(battery_icon);
            }
            bool recording=atomic_load(&capturing);
            update_wave(recording,atomic_load(&meter));
            int seconds=recording?(int)((xTaskGetTickCount()-atomic_load(&capture_started))/configTICK_RATE_HZ):0;
            if(seconds!=displayed_seconds) {
                lv_label_set_text_fmt(time_label,"%02d:%02d / 01:00",seconds/60,seconds%60);
                displayed_seconds=seconds;
            }
            bsp_lvgl_unlock();
        }
        app_pages_save_preferences(now);
        if(esp_timer_get_time()-last_report>10000000) {
            ESP_LOGI(TAG,"heap=%u largest=%u",(unsigned)esp_get_free_heap_size(),(unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
            last_report=esp_timer_get_time();
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
