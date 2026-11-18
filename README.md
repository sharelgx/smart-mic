# Smart Mic

用 FoloToy AI Passport 的实体按键控制 macOS 上的 Codex 聊天。按一次第三键开始搜狗听写，再按一次结束并发送。屏幕显示 `SENT` 表示当前聊天已收到消息。

## 首次部署

准备一台 Mac、FoloToy AI Passport（ESP32-C3、8 MB）、USB 数据线，以及设备能连接的 2.4 GHz Wi-Fi。目前在 Apple Silicon Mac 上验证过桥接流程。

1. 安装 Python 3.12、Codex 桌面应用、搜狗语音助手和 BlackHole 2ch。在 macOS「系统设置 → 声音 → 输入」选择 BlackHole 2ch。
2. 把项目放在固定目录，双击 **部署 Smart Mic.command**（也可运行 `./部署\ Smart\ Mic.command`）。它会打开本机浏览器向导。依次通过 Mac 检查、USB 检测、Wi-Fi 配对和登录启动设置。
3. 到「首次发送」时，在目标本地聊天按 ⌘⌥L，再按设备第三键开始听写。第二次按键发送，等设备显示 `SENT` 并确认文字进入目标聊天，再在向导里完成部署。

刷机需要现有分区表与随附固件匹配。程序会覆盖应用分区，请在确认板型并接受覆盖后勾选刷机确认框。已经刷好 Smart Mic 固件的设备可直接配对。配对成功后可拔下 USB。Mac 局域网地址变更时，用 USB 接回设备并重跑部署入口。

桥接与 Codex 共享启动器在登录后启动。Wi-Fi 密码通过 USB 写入设备，Mac 仅保存本地配对密钥。聊天绑定和配对文件都在项目的 `work/private/` 下。项目目录移动后重新执行部署入口。

## 源码

- `bridge/` 包含 Wi-Fi 音频桥、虚拟麦克风、听写会话与聊天绑定。
- `tools/` 包含部署工具及桌面共享接口启动器。
- `firmware/ai-passport/` 包含板端工程、上游许可及字体许可。
- `release/` 包含 ESP32-C3 应用固件、匹配的分区表和 SHA-256 清单。

搜狗与 Codex 升级后需要复验听写和发送。当前发布版的实体设备部署流程仍待在另一台干净 Mac 和第二块同型号板上实测。
