#!/bin/bash
# 一键安装 wrapper：读 .env → 调 install.py
#   ./install.sh --dry-run
#   ./install.sh --only portable
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "缺少 .env。先执行：cp config/.env.example .env && \$EDITOR .env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

exec python3 install.py "$@"
