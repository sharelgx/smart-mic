# 优特利 500 mAh 板的固件更新

这块 FoloToy AI Passport 已通过 CW2017 参数回读和屏幕 96% 显示验证。更新这块板时，激活 ESP-IDF 5.5.3 后执行 `./tools/build-youteli-passport.sh`。脚本每次为应用与 BSP 启用同一电池配置，使用独立构建目录。

应用镜像位于 `work/build-youteli-passport/FoloToy-AI-Passport.bin`。写入前核对目标设备与分区表。更新后用串口发送 `@PVM1 BATTERY`，核对 `profile_match=1`、`soc` 与 `accepted` 一致且均在 0 至 100 之间，并检查屏幕百分比。

本版在设备上播放对应 Codex 轮次的最终回复，板端播放音量为 100%。Mac 使用仓库中的 `bridge/wifi_microphone.py` 和 `requirements.txt`。随附的 `release/smart-mic.bin` 是实机验收的应用镜像，SHA-256 见 `release/SHA256SUMS`。重新编译时，ESP-IDF 构建元数据可能改变二进制哈希。
