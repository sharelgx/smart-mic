#!/bin/zsh
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd -- "${0:A:h}"
if command -v python3.12 >/dev/null; then
  exec python3.12 tools/wizard.py
elif command -v python3 >/dev/null; then
  exec python3 tools/wizard.py
fi
/usr/bin/osascript -e 'display alert "请先安装 Python 3.12" message "安装后再次双击部署 Smart Mic.command。"'
/usr/bin/open https://www.python.org/downloads/macos/
