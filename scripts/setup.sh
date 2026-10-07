#!/usr/bin/env bash
# 无 root 环境准备：pip 包 + 用户目录 PostgreSQL + npm 依赖
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

python3 -m pip --version >/dev/null 2>&1 || python3 /tmp/get-pip.py --user --break-system-packages
python3 -m pip install --user --break-system-packages -r "$ROOT/backend/requirements.txt"

if [ ! -x "$HOME/pg/usr/lib/postgresql/15/bin/postgres" ]; then
  mkdir -p /tmp/pgdebs && cd /tmp/pgdebs
  BASE="http://ftp.debian.org/debian/pool/main/p/postgresql-15"
  VER="15.18-0+deb12u1"
  for f in postgresql-15_${VER}_arm64.deb postgresql-client-15_${VER}_arm64.deb libpq5_${VER}_arm64.deb; do
    [ -f "$f" ] || curl -fSLO "$BASE/$f"
  done
  mkdir -p "$HOME/pg"
  for f in *.deb; do dpkg-deb -x "$f" "$HOME/pg"; done
fi

cd "$ROOT/frontend"
[ -d node_modules/vite ] || npm install --no-audit --no-fund
echo "setup complete"
