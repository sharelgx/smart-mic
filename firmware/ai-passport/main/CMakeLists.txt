idf_component_register(
    SRCS "wifi_mic.c" "mic_wire.c"
         "../assets/fonts/passport_ui_16.c" "../assets/fonts/passport_ui_24.c"
    INCLUDE_DIRS "."
    REQUIRES bsp esp_event esp_netif esp_timer esp_wifi nvs_flash json mbedtls esp_driver_usb_serial_jtag
)
