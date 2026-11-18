#pragma once
#include <stdint.h>
#include <stddef.h>
#include "mbedtls/gcm.h"

enum { MIC_START=1, MIC_AUDIO, MIC_END, MIC_CANCEL, MIC_RESULT, MIC_READY };
typedef struct {
    int fd;
    uint64_t tx, rx;
    mbedtls_gcm_context up, down;
} mic_wire_t;
int mic_wire_open(mic_wire_t *wire, int fd, const uint8_t key[32]);
int mic_wire_send(mic_wire_t *wire, uint8_t kind, const void *data, size_t size);
int mic_wire_read(mic_wire_t *wire, uint8_t *kind, void *data, size_t capacity);
void mic_wire_free(mic_wire_t *wire);
