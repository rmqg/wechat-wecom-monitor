#!/bin/sh
# 离线安装入口：不下载、不安装依赖、不接触账号。
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    printf '%s\n' '未找到 Python。请先准备 Python 3.10+，然后重新运行安装器。' >&2
    printf '%s\n' 'macOS（已安装 Homebrew）：brew install python' 'Debian/Ubuntu：sudo apt-get install python3 python3-venv python3-pip' '也可从 https://www.python.org/downloads/ 手动安装；本安装器不会联网。' >&2
    exit 1
fi
exec "$PYTHON" "$ROOT/scripts/install.py" "$@"
