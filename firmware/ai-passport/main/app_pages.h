#pragma once

#include "lvgl.h"
#include "bsp_button.h"
#include <stdint.h>

typedef enum { PAGE_MENU, PAGE_VOICE, PAGE_TIMER } app_page_t;

// Call on the app task after NVS initialization and outside the LVGL lock.
void app_pages_load_preferences(void);
void app_pages_save_preferences(int64_t now);

// Calls run with the board's LVGL lock held.
void app_pages_init(lv_obj_t *voice_screen);
app_page_t app_pages_key(app_page_t page, bsp_btn_t btn, bsp_btn_ev_t event);
void app_pages_tick(int64_t now);
