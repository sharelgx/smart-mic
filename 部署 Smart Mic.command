#!/bin/zsh
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  if ! command -v python3.12 >/dev/null; then
    print '请安装 Python 3.12，再打开本部署程序。'
    exit 1
  fi
  python3.12 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python tools/deploy.py
