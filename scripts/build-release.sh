#!/bin/sh
# 只打包提交中的文件；不下载依赖、不包含工作区中的私有配置。
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
PYTHON=${PYTHON:-python3}
if ! command -v git >/dev/null 2>&1; then
    printf '%s\n' '缺少 git；请先安装 git，再运行发布脚本。' >&2
    exit 1
fi
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    printf '%s\n' '缺少 Python 3.10+；设置 PYTHON=/path/to/python3 后重试。' >&2
    exit 1
fi
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "需要 Python 3.10+")'
VERSION=$(cat VERSION)
"$PYTHON" - "$VERSION" <<'PY'
import re
import sys
if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', sys.argv[1]) is None:
    raise SystemExit('VERSION 必须为三段数字版本号，如 0.1.0')
PY
if ! git diff --quiet HEAD --; then
    printf '%s\n' '存在未提交的已跟踪修改；请先提交，再构建发布包。' >&2
    exit 1
fi
COMMITTED_VERSION=$(git show HEAD:VERSION)
if [ "$COMMITTED_VERSION" != "$VERSION" ]; then
    printf '%s\n' '提交中的 VERSION 与工作区不同。' >&2
    exit 1
fi
"$PYTHON" scripts/audit-release.py
NAME="wechat-wecom-monitor-$VERSION"
mkdir -p dist
TMP=$(mktemp -d "$ROOT/dist/.release.XXXXXX")
trap 'rm -rf "$TMP"' 0
trap 'exit 1' HUP INT TERM
git archive --format=tar --prefix="$NAME/" --output="$TMP/source.tar" HEAD
gzip -n -c "$TMP/source.tar" > "$TMP/$NAME.tar.gz"
"$PYTHON" - "$TMP/$NAME.tar.gz" "$TMP/SHA256SUMS" <<'PY'
import hashlib
from pathlib import Path
import sys
archive, sums = map(Path, sys.argv[1:])
digest = hashlib.sha256()
with archive.open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
sums.write_text(f'{digest.hexdigest()}  {archive.name}\n', encoding='ascii')
PY
mv "$TMP/$NAME.tar.gz" "dist/$NAME.tar.gz"
mv "$TMP/SHA256SUMS" dist/SHA256SUMS
printf '%s\n' "dist/$NAME.tar.gz" 'dist/SHA256SUMS'
