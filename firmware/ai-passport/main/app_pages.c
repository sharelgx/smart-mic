#include "app_pages.h"
#include "pomodoro.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "nvs.h"
#include <string.h>

LV_FONT_DECLARE(passport_ui_16);

static const uint32_t background = 0x080E0B;
static const uint32_t foreground = 0xF0F7F2;
static const uint32_t muted = 0xACBBB1;
static const uint32_t tomato = 0xFF927B;
static const uint32_t mint = 0x72F1A1;

enum { FRUIT_REST_Y = 50 };
typedef enum { FRUIT_STILL, FRUIT_BOUNCE, FRUIT_CELEBRATE } fruit_motion_t;
static lv_obj_t *menu, *voice, *clock_screen, *fruit;
static fruit_motion_t motion;
static bool completion_seen;
static int fruit_height;
typedef enum { CLOCK_TOMATO = 0, CLOCK_DIAL = 1, CLOCK_FLIP = 2,
               CLOCK_HOURGLASS = 3, CLOCK_STYLE_COUNT } clock_style_t;
static const char *const preferences_namespace = "passport_clock";
static const char *const style_key = "style";
static clock_style_t face_style;
static clock_style_t saved_style;
static int64_t next_save_attempt;
static unsigned style_cursor;
static bool selecting_style;
static int grain;
static lv_obj_t *style_screen, *style_rows[CLOCK_STYLE_COUNT];
static lv_obj_t *clock_title, *shadow, *clock_face, *flip_panel, *clock_tip, *clock_back;
static lv_obj_t *flip_digits[4], *dial_numbers[4];
static lv_obj_t *rows[2], *phase, *digits, *progress, *action;
static unsigned selected;
static pomodoro_t timer;
static int shown_seconds = -1;
static const char *shown_phase, *shown_action;

void app_pages_load_preferences(void) {
    nvs_handle_t preferences;
    esp_err_t status = nvs_open(preferences_namespace, NVS_READONLY, &preferences);
    uint8_t stored = CLOCK_TOMATO;
    if(status == ESP_OK) {
        status = nvs_get_u8(preferences, style_key, &stored);
        nvs_close(preferences);
    }
    if(status == ESP_OK && stored < CLOCK_STYLE_COUNT)
        face_style = (clock_style_t)stored;
    else
        face_style = CLOCK_TOMATO;
    saved_style = face_style;
}

void app_pages_save_preferences(int64_t now) {
    if(face_style == saved_style || now < next_save_attempt) return;

    nvs_handle_t preferences;
    esp_err_t status = nvs_open(preferences_namespace, NVS_READWRITE, &preferences);
    if(status == ESP_OK) {
        status = nvs_set_u8(preferences, style_key, (uint8_t)face_style);
        if(status == ESP_OK) status = nvs_commit(preferences);
        nvs_close(preferences);
    }
    if(status == ESP_OK) {
        saved_style = face_style;
        next_save_attempt = 0;
    } else {
        ESP_LOGW("clock", "could not save clock style: %s", esp_err_to_name(status));
        next_save_attempt = now + 5000000;
    }
}

static lv_obj_t *make_screen(void) {
    lv_obj_t *screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(screen, lv_color_hex(background), 0);
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    return screen;
}

static lv_obj_t *make_label(lv_obj_t *screen, const char *text, int y,
                            const lv_font_t *font, uint32_t color) {
    lv_obj_t *label = lv_label_create(screen);
    lv_obj_set_width(label, 204);
    lv_obj_set_pos(label, 18, y);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_font(label, font, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
    lv_label_set_text(label, text);
    return label;
}

static void draw_pixel_block(lv_layer_t *layer, lv_draw_rect_dsc_t *ink,
                             int x, int y, int width, int height, uint32_t color) {
    lv_area_t box = {x, y, x + width - 1, y + height - 1};
    ink->bg_color = lv_color_hex(color);
    lv_draw_rect(layer, ink, &box);
}

static void draw_face(lv_layer_t *layer, lv_draw_rect_dsc_t *ink, int x, int y) {
    const uint32_t dark = 0x54252C;
    const bool happy = motion == FRUIT_CELEBRATE;
    const bool blinking = motion == FRUIT_BOUNCE && fruit_height >= 8;
    for(int eye = 0; eye < 2; ++eye) {
        int eye_x = x + 44 + eye * 40;
        if(happy || blinking) {
            draw_pixel_block(layer, ink, eye_x, y + 62, 12, 4, dark);
            if(happy) draw_pixel_block(layer, ink, eye_x + 4, y + 58, 4, 4, dark);
        } else {
            draw_pixel_block(layer, ink, eye_x, y + 55, 8, 12, dark);
            draw_pixel_block(layer, ink, eye_x + 4, y + 55, 4, 4, 0xFFF0D6);
        }
    }
    draw_pixel_block(layer, ink, x + 32, y + 73, 8, 4, 0xFF987E);
    draw_pixel_block(layer, ink, x + 96, y + 73, 8, 4, 0xFF987E);
    draw_pixel_block(layer, ink, x + 60, y + 77, 16, happy ? 12 : 4, dark);
    if(happy) draw_pixel_block(layer, ink, x + 64, y + 85, 8, 4, 0xFFBA9F);
    else {
        draw_pixel_block(layer, ink, x + 56, y + 73, 4, 4, dark);
        draw_pixel_block(layer, ink, x + 76, y + 73, 4, 4, dark);
    }
}

static void draw_confetti(lv_layer_t *layer, lv_draw_rect_dsc_t *ink, int x, int y) {
    static const struct { int x, y; uint32_t color; } pieces[] = {
        {9, 38, 0xFFD269}, {20, 64, 0x83E7B1}, {2, 80, 0xFF987E},
        {171, 35, 0x83E7B1}, {181, 55, 0xFFD269}, {169, 79, 0xFF987E},
    };
    for(unsigned i = 0; i < sizeof(pieces) / sizeof(pieces[0]); ++i) {
        draw_pixel_block(layer, ink, x + pieces[i].x, y + pieces[i].y - fruit_height / 2,
                         6, 6, pieces[i].color);
    }
}

// 17 × 13 sprite. Draw contiguous runs to keep the LVGL draw list small.
static void draw_tomato(lv_event_t *event) {
    static const char sprite[][18] = {
        ".........G.......",
        ".....G...gg......",
        "......G.gg.GG....",
        "....rRGgggGRr....",
        "...rRLRGgGRRRr...",
        "..rRLLRRGRRRRDr..",
        ".rRLRRRRRRRRRDDr.",
        ".rRLRRRRRRRRRDDr.",
        ".rRRRRRRRRRRRDDr.",
        ".rRRRRRRRRRRDDr..",
        "..rRRRRRRRDDDr...",
        "...rRDDDDDDDr....",
        ".....rrrrrr......",
    };
    static const char tones[] = "GgrRLD";
    static const uint32_t colors[] = {
        0x49753C, 0x8EBD5C, 0xA93636, 0xF35647, 0xFFA092, 0xCE3E3A,
    };
    enum { SCALE = 8 };
    lv_area_t bounds;
    lv_obj_get_coords(lv_event_get_target_obj(event), &bounds);
    lv_layer_t *layer = lv_event_get_layer(event);
    lv_draw_rect_dsc_t ink;
    lv_draw_rect_dsc_init(&ink);
    const int fruit_x = bounds.x1 + 28;
    for (unsigned y = 0; y < sizeof(sprite) / sizeof(sprite[0]); ++y) {
        for (unsigned x = 0; x < 17;) {
            unsigned end = x + 1;
            while (end < 17 && sprite[y][x] == sprite[y][end]) ++end;
            for (unsigned color = 0; color < sizeof(colors) / sizeof(colors[0]); ++color) {
                if (sprite[y][x] != tones[color]) continue;
                lv_area_t run = {
                    fruit_x + (int)x * SCALE, bounds.y1 + (int)y * SCALE,
                    fruit_x + (int)end * SCALE - 1, bounds.y1 + (int)(y + 1) * SCALE - 1,
                };
                ink.bg_color = lv_color_hex(colors[color]);
                lv_draw_rect(layer, &ink, &run);
                break;
            }
            x = end;
        }
    }
    draw_face(layer, &ink, fruit_x, bounds.y1);
    if(motion == FRUIT_CELEBRATE) draw_confetti(layer, &ink, bounds.x1, bounds.y1);
}

static void fruit_y(void *target, int32_t y) {
    fruit_height = FRUIT_REST_Y - y;
    lv_obj_set_y(target, y);
    lv_obj_invalidate(target);
}

static void celebrate_done(lv_anim_t *animation) {
    (void)animation;
    motion = FRUIT_STILL;
    fruit_y(fruit, FRUIT_REST_Y);
}

static void animate_fruit(fruit_motion_t next) {
    if(next == motion) return;
    lv_anim_delete(fruit, fruit_y);
    motion = next;
    fruit_y(fruit, FRUIT_REST_Y);
    if(next == FRUIT_STILL) return;

    bool celebrating = next == FRUIT_CELEBRATE;
    lv_anim_t hop;
    lv_anim_init(&hop);
    lv_anim_set_var(&hop, fruit);
    lv_anim_set_exec_cb(&hop, fruit_y);
    lv_anim_set_values(&hop, FRUIT_REST_Y, FRUIT_REST_Y - (celebrating ? 16 : 10));
    lv_anim_set_duration(&hop, celebrating ? 210 : 270);
    lv_anim_set_reverse_duration(&hop, celebrating ? 245 : 310);
    lv_anim_set_path_cb(&hop, lv_anim_path_ease_in_out);
    lv_anim_set_repeat_count(&hop, celebrating ? 3 : LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_repeat_delay(&hop, celebrating ? 75 : 950);
    if(celebrating) lv_anim_set_completed_cb(&hop, celebrate_done);
    lv_anim_start(&hop);
}

static void clock_line(lv_layer_t *layer, int x1, int y1, int x2, int y2,
                       int width, uint32_t color) {
    lv_draw_line_dsc_t line;
    lv_draw_line_dsc_init(&line);
    line.p1.x = x1;
    line.p1.y = y1;
    line.p2.x = x2;
    line.p2.y = y2;
    line.width = width;
    line.color = lv_color_hex(color);
    lv_draw_line(layer, &line);
}

static int dial_offset(int radius, int angle) {
    return radius * lv_trigo_sin((int16_t)angle) / (1 << LV_TRIGO_SHIFT);
}

static void draw_dial(lv_layer_t *layer, int cx, int cy) {
    lv_draw_arc_dsc_t arc;
    lv_draw_arc_dsc_init(&arc);
    arc.center.x = cx;
    arc.center.y = cy;
    arc.radius = 86;
    arc.width = 2;
    arc.color = lv_color_hex(0xBDAA83);
    arc.end_angle = 360;
    lv_draw_arc(layer, &arc);
    const int angle = (int)(360 * timer.remaining_us / pomodoro_duration(&timer));
    if(angle > 0) {
        arc.radius = 80;
        arc.width = 5;
        arc.start_angle = 270;
        arc.end_angle = 270 + angle;
        arc.color = lv_color_hex(timer.resting ? 0x49845B : 0xBC5733);
        lv_draw_arc(layer, &arc);
    }
    for(int i = 0; i < 60; ++i) {
        const int tick = i * 6;
        const int inner = i % 5 == 0 ? 64 : 71;
        clock_line(layer, cx + dial_offset(inner, tick), cy - dial_offset(inner, tick + 90),
                   cx + dial_offset(75, tick), cy - dial_offset(75, tick + 90),
                   i % 5 == 0 ? 2 : 1, 0x4D4237);
    }
    clock_line(layer, cx, cy, cx + dial_offset(58, angle), cy - dial_offset(58, angle + 90),
               4, timer.resting ? 0x49845B : 0xBC5733);
    lv_draw_rect_dsc_t hub;
    lv_draw_rect_dsc_init(&hub);
    hub.radius = LV_RADIUS_CIRCLE;
    draw_pixel_block(layer, &hub, cx - 4, cy - 4, 9, 9, 0x46362A);
}

static void draw_hourglass(lv_layer_t *layer, int cx, int top) {
    lv_draw_rect_dsc_t ink;
    lv_draw_rect_dsc_init(&ink);
    draw_pixel_block(layer, &ink, cx - 46, top, 92, 7, 0xAF82CF);
    draw_pixel_block(layer, &ink, cx - 46, top + 142, 92, 7, 0xAF82CF);
    int remaining_rows = (int)(15 * timer.remaining_us / pomodoro_duration(&timer));
    const uint32_t sand = timer.resting ? 0x9EF1BB : 0xF8C36D;
    for(int row = 0; row < 34; ++row) {
        int half = 6 + LV_ABS(row * 4 - 67) * 56 / 100;
        int y = top + 7 + row * 4;
        draw_pixel_block(layer, &ink, cx - half, y, half * 2, 4, 0x382342);
        draw_pixel_block(layer, &ink, cx - half, y, 3, 4, 0xAF82CF);
        draw_pixel_block(layer, &ink, cx + half - 3, y, 3, 4, 0xAF82CF);
        if((row < 16 && row >= 16 - remaining_rows) || row >= 34 - (15 - remaining_rows))
            draw_pixel_block(layer, &ink, cx - half + 4, y, half * 2 - 8, 4, sand);
    }
    if(timer.deadline_us)
        draw_pixel_block(layer, &ink, cx - 2, top + 77 + grain * 4, 4, 4, sand);
}

static void draw_clock_face(lv_event_t *event) {
    lv_area_t bounds;
    lv_obj_get_coords(lv_event_get_target_obj(event), &bounds);
    lv_layer_t *layer = lv_event_get_layer(event);
    if(face_style == CLOCK_DIAL) draw_dial(layer, bounds.x1 + 96, bounds.y1 + 88);
    if(face_style == CLOCK_HOURGLASS) draw_hourglass(layer, bounds.x1 + 96, bounds.y1 + 8);
}

static void flip_scale(void *object, int32_t scale) {
    lv_obj_set_style_transform_scale_y(object, scale, 0);
}

static void display_flip_digits(int seconds, bool animate) {
    int minutes = seconds / 60;
    int remainder = seconds % 60;
    const char value[4] = {
        '0' + minutes / 10, '0' + minutes % 10,
        '0' + remainder / 10, '0' + remainder % 10,
    };
    static char last[4];
    for(unsigned i = 0; i < 4; ++i) {
        if(value[i] == last[i] && animate) continue;
        lv_label_set_text_fmt(flip_digits[i], "%c", value[i]);
        lv_anim_delete(flip_digits[i], flip_scale);
        if(animate) {
            lv_anim_t flip;
            lv_anim_init(&flip);
            lv_anim_set_var(&flip, flip_digits[i]);
            lv_anim_set_exec_cb(&flip, flip_scale);
            lv_anim_set_values(&flip, 35, 256);
            lv_anim_set_duration(&flip, 190);
            lv_anim_set_path_cb(&flip, lv_anim_path_ease_out);
            lv_anim_start(&flip);
        } else {
            flip_scale(flip_digits[i], 256);
        }
    }
    memcpy(last, value, sizeof(last));
}

static void set_visible(lv_obj_t *object, bool visible) {
    if(visible) lv_obj_remove_flag(object, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(object, LV_OBJ_FLAG_HIDDEN);
}

static void apply_clock_style(void) {
    static const uint32_t backgrounds[] = {0x170E10, 0xF1E5CA, 0x0A1625, 0x21122D};
    static const uint32_t inks[] = {0xF0F7F2, 0x44352B, 0xE8F4FC, 0xFFF0FA};
    static const uint32_t secondary[] = {0xACBBB1, 0x5D5243, 0xA8BECE, 0xDEBFED};
    bool character = face_style == CLOCK_TOMATO;
    lv_obj_set_style_bg_color(clock_screen, lv_color_hex(backgrounds[face_style]), 0);
    lv_obj_set_style_text_color(digits, lv_color_hex(inks[face_style]), 0);
    lv_obj_set_style_text_color(action, lv_color_hex(inks[face_style]), 0);
    lv_obj_set_style_text_color(clock_tip, lv_color_hex(secondary[face_style]), 0);
    lv_obj_set_style_text_color(clock_back, lv_color_hex(secondary[face_style]), 0);
    lv_obj_set_y(phase, character ? 176 : 20);
    lv_obj_set_y(digits, character ? 195 : 216);
    lv_obj_set_style_text_font(digits, character ? &lv_font_montserrat_48 : &lv_font_montserrat_28, 0);
    lv_obj_set_y(progress, character ? 251 : 213);
    lv_obj_set_style_bg_color(progress, lv_color_hex(character ? 0x43292C : 0x25374B), 0);
    set_visible(clock_title, character);
    set_visible(fruit, character);
    set_visible(shadow, character);
    set_visible(digits, face_style != CLOCK_FLIP);
    set_visible(progress, character || face_style == CLOCK_FLIP);
    set_visible(clock_face, face_style == CLOCK_DIAL || face_style == CLOCK_HOURGLASS);
    set_visible(flip_panel, face_style == CLOCK_FLIP);
    for(unsigned i = 0; i < 4; ++i) set_visible(dial_numbers[i], face_style == CLOCK_DIAL);
    shown_seconds = -1;
}

static void refresh_style_list(void) {
    for(unsigned i = 0; i < CLOCK_STYLE_COUNT; ++i) {
        lv_obj_set_style_bg_opa(style_rows[i], i == style_cursor ? LV_OPA_COVER : LV_OPA_TRANSP, 0);
        lv_obj_set_style_text_color(style_rows[i], lv_color_hex(i == style_cursor ? mint : foreground), 0);
    }
}

static void close_style_list(bool apply) {
    selecting_style = false;
    if(apply) face_style = style_cursor;
    apply_clock_style();
    lv_screen_load(clock_screen);
    app_pages_tick(esp_timer_get_time());
}

static void refresh_menu(void) {
    for (unsigned i = 0; i < 2; ++i) {
        lv_obj_set_style_bg_opa(rows[i], i == selected ? LV_OPA_COVER : LV_OPA_TRANSP, 0);
        lv_obj_set_style_text_color(rows[i], lv_color_hex(i == selected ? mint : foreground), 0);
    }
}

void app_pages_init(lv_obj_t *voice_screen) {
    voice = voice_screen;
    menu = make_screen();
    make_label(menu, "应用列表", 42, &passport_ui_16, mint);
    rows[0] = make_label(menu, "语音控制", 95, &passport_ui_16, foreground);
    rows[1] = make_label(menu, "番茄时钟", 157, &passport_ui_16, foreground);
    for (unsigned i = 0; i < 2; ++i) {
        lv_obj_set_style_bg_color(rows[i], lv_color_hex(0x18352A), 0);
        lv_obj_set_style_radius(rows[i], 8, 0);
        lv_obj_set_style_pad_ver(rows[i], 13, 0);
    }
    make_label(menu, "上下选择 · 确定进入", 250, &passport_ui_16, muted);
    refresh_menu();

    clock_screen = make_screen();
    lv_obj_set_style_bg_color(clock_screen, lv_color_hex(0x170E10), 0);
    clock_title=make_label(clock_screen, "番茄时钟", 20, &passport_ui_16, muted);
    shadow = lv_obj_create(clock_screen);
    lv_obj_remove_style_all(shadow);
    lv_obj_remove_flag(shadow, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_pos(shadow, 84, 162);
    lv_obj_set_size(shadow, 72, 8);
    lv_obj_set_style_bg_color(shadow, lv_color_hex(0x372427), 0);
    lv_obj_set_style_bg_opa(shadow, LV_OPA_COVER, 0);
    fruit = lv_obj_create(clock_screen);
    lv_obj_remove_style_all(fruit);
    lv_obj_remove_flag(fruit, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(fruit, 192, 104);
    lv_obj_set_pos(fruit, 24, FRUIT_REST_Y);
    lv_obj_add_event_cb(fruit, draw_tomato, LV_EVENT_DRAW_MAIN, NULL);
    phase = make_label(clock_screen, "", 176, &passport_ui_16, tomato);
    digits = make_label(clock_screen, "", 195, &lv_font_montserrat_48, foreground);
    progress = lv_bar_create(clock_screen);
    lv_obj_set_size(progress, 164, 6);
    lv_obj_set_pos(progress, 38, 251);
    lv_obj_set_style_bg_color(progress, lv_color_hex(0x43292C), 0);
    action = make_label(clock_screen, "", 263, &passport_ui_16, foreground);
    clock_tip = make_label(clock_screen, "上键样式 · 下键重置", 283, &passport_ui_16, muted);
    clock_back = make_label(clock_screen, "长按上键返回", 301, &passport_ui_16, muted);

    clock_face = lv_obj_create(clock_screen);
    lv_obj_remove_style_all(clock_face);
    lv_obj_set_size(clock_face, 192, 176);
    lv_obj_set_pos(clock_face, 24, 42);
    lv_obj_add_event_cb(clock_face, draw_clock_face, LV_EVENT_DRAW_MAIN, NULL);
    static const char *markers[] = {"12", "3", "6", "9"};
    static const int marker_x[] = {105, 156, 105, 54};
    static const int marker_y[] = {70, 121, 173, 121};
    for(unsigned i = 0; i < 4; ++i) {
        dial_numbers[i] = make_label(clock_screen, markers[i], marker_y[i],
                                     &lv_font_montserrat_14, 0x46362A);
        lv_obj_set_width(dial_numbers[i], 30);
        lv_obj_set_x(dial_numbers[i], marker_x[i]);
    }

    flip_panel = lv_obj_create(clock_screen);
    lv_obj_remove_style_all(flip_panel);
    lv_obj_set_size(flip_panel, 240, 88);
    lv_obj_set_pos(flip_panel, 0, 99);
    static const int card_x[] = {18, 65, 129, 176};
    for(unsigned i = 0; i < 4; ++i) {
        lv_obj_t *card = lv_obj_create(flip_panel);
        lv_obj_remove_style_all(card);
        lv_obj_set_size(card, 45, 85);
        lv_obj_set_pos(card, card_x[i], 1);
        lv_obj_set_style_bg_color(card, lv_color_hex(0x1B3149), 0);
        lv_obj_set_style_bg_opa(card, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(card, 6, 0);
        flip_digits[i] = make_label(card, "", 15, &lv_font_montserrat_48, 0xE8F4FC);
        lv_obj_set_width(flip_digits[i], 45);
        lv_obj_set_x(flip_digits[i], 0);
        lv_obj_set_style_transform_pivot_y(flip_digits[i], lv_pct(50), 0);
        lv_obj_t *seam = lv_obj_create(card);
        lv_obj_remove_style_all(seam);
        lv_obj_set_size(seam, 43, 2);
        lv_obj_set_pos(seam, 1, 42);
        lv_obj_set_style_bg_color(seam, lv_color_hex(0x0B1929), 0);
        lv_obj_set_style_bg_opa(seam, LV_OPA_COVER, 0);
    }
    make_label(flip_panel, ":", 16, &lv_font_montserrat_48, 0x66DAFA);

    style_screen = make_screen();
    make_label(style_screen, "时钟样式", 26, &passport_ui_16, mint);
    static const char *styles[] = {"像素番茄", "指针表盘", "机械翻页", "像素沙漏"};
    for(unsigned i = 0; i < CLOCK_STYLE_COUNT; ++i) {
        style_rows[i] = make_label(style_screen, styles[i], 59 + 49 * (int)i, &passport_ui_16, foreground);
        lv_obj_set_style_bg_color(style_rows[i], lv_color_hex(0x18352A), 0);
        lv_obj_set_style_radius(style_rows[i], 8, 0);
        lv_obj_set_style_pad_ver(style_rows[i], 11, 0);
    }
    make_label(style_screen, "上下选择 · 确定切换", 265, &passport_ui_16, muted);
    make_label(style_screen, "长按上键取消", 289, &passport_ui_16, muted);
    apply_clock_style();
    pomodoro_reset(&timer);
    lv_screen_load(menu);
}

app_page_t app_pages_key(app_page_t page, bsp_btn_t btn, bsp_btn_ev_t event) {
    if(page == PAGE_TIMER && selecting_style) {
        if(btn == BSP_BTN_UP && event == BSP_BTN_LONG) close_style_list(false);
        if(event == BSP_BTN_CLICK) {
            if(btn == BSP_BTN_OK) close_style_list(true);
            if(btn == BSP_BTN_UP || btn == BSP_BTN_DOWN) {
                unsigned step = btn == BSP_BTN_DOWN ? 1 : CLOCK_STYLE_COUNT - 1;
                style_cursor = (style_cursor + step) % CLOCK_STYLE_COUNT;
                refresh_style_list();
            }
        }
        return page;
    }
    if (page != PAGE_MENU && btn == BSP_BTN_UP && event == BSP_BTN_LONG) {
        if(page == PAGE_TIMER) animate_fruit(FRUIT_STILL);
        lv_screen_load(menu);
        return PAGE_MENU;
    }
    if (event != BSP_BTN_CLICK) return page;
    if (page == PAGE_MENU) {
        if (btn == BSP_BTN_OK) {
            if (selected == 0) {
                lv_screen_load(voice);
                return PAGE_VOICE;
            }
            app_pages_tick(esp_timer_get_time());
            lv_screen_load(clock_screen);
            return PAGE_TIMER;
        }
        if (btn == BSP_BTN_UP || btn == BSP_BTN_DOWN) selected ^= 1U;
        refresh_menu();
    } else if (page == PAGE_TIMER) {
        if(btn == BSP_BTN_UP) {
            selecting_style = true;
            style_cursor = face_style;
            animate_fruit(FRUIT_STILL);
            refresh_style_list();
            lv_screen_load(style_screen);
            return page;
        }
        int64_t now = esp_timer_get_time();
        if (btn == BSP_BTN_OK) pomodoro_toggle(&timer, now);
        if (btn == BSP_BTN_DOWN) pomodoro_reset(&timer);
        app_pages_tick(now);
    }
    return page;
}

void app_pages_tick(int64_t now) {
    pomodoro_update(&timer, now);
    if(selecting_style) return;
    if(face_style == CLOCK_TOMATO) {
        if(timer.finished) {
            if(!completion_seen) animate_fruit(FRUIT_CELEBRATE);
        } else {
            animate_fruit(timer.deadline_us ? FRUIT_BOUNCE : FRUIT_STILL);
        }
    }
    completion_seen = timer.finished;
    const char *phase_text = timer.finished ? "已完成" : timer.resting ? "休息" : "专注";
    const char *action_text = timer.finished ? (timer.resting ? "确定开始专注" : "确定开始休息")
        : timer.deadline_us ? "确定暂停" : "确定开始";
    int seconds = (int)((timer.remaining_us + 999999) / 1000000);
    if (shown_phase != phase_text || shown_seconds < 0) {
        shown_phase = phase_text;
        lv_label_set_text(phase, phase_text);
        static const uint32_t focus_colors[] = {0xFF927B, 0xA34C29, 0x66DAFA, 0xF8C36D};
        static const uint32_t rest_colors[] = {0x72F1A1, 0x367147, 0x9EF1BB, 0x9EF1BB};
        lv_color_t accent = lv_color_hex(timer.resting ? rest_colors[face_style] : focus_colors[face_style]);
        lv_obj_set_style_text_color(phase, accent, 0);
        lv_obj_set_style_bg_color(progress, accent, LV_PART_INDICATOR);
    }
    if (shown_action != action_text) {
        shown_action = action_text;
        lv_label_set_text(action, action_text);
    }
    if (shown_seconds != seconds) {
        if(face_style == CLOCK_FLIP) display_flip_digits(seconds, shown_seconds >= 0 && timer.deadline_us);
        shown_seconds = seconds;
        lv_obj_invalidate(clock_face);
        lv_label_set_text_fmt(digits, "%02d:%02d", seconds / 60, seconds % 60);
    }
    if(face_style == CLOCK_HOURGLASS) {
        int next_grain = timer.deadline_us ? (int)((now / 100000) % 10) : -1;
        if(grain != next_grain) {
            grain = next_grain;
            lv_obj_invalidate(clock_face);
        }
    }
    const int percent = (int)(100 * timer.remaining_us / pomodoro_duration(&timer));
    if (lv_bar_get_value(progress) != percent) lv_bar_set_value(progress, percent, LV_ANIM_OFF);
}
