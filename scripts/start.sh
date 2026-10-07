#!/usr/bin/env bash
# 一键启动本地开发环境：PostgreSQL（用户目录）+ FastAPI + Vite
set -euo pipefail

PGHOME="$HOME/pg"
PGBIN="$PGHOME/usr/lib/postgresql/15/bin"
export LD_LIBRARY_PATH="$PGHOME/usr/lib/postgresql/15/lib:$PGHOME/usr/lib/aarch64-linux-gnu${LD_LIBRARY_PATH:-}"
export PATH="$HOME/.local/bin:$PGBIN:$PATH"
export DATABASE_URL="postgresql+psycopg2://elevator@/elevator_lab?host=/tmp"

ROOT="$(cd "$(dirname "$0")" && pwd)"

# 1) 若本机 5432 没有 PostgreSQL，则用用户目录内的实例
if ! pg_isready -h /tmp -p 5432 >/dev/null 2>&1; then
  if [ ! -x "$PGBIN/postgres" ]; then
    echo "未找到 PostgreSQL，请先运行 scripts/setup.sh" >&2
    exit 1
  fi
  if [ ! -s "$HOME/pgdata/PG_VERSION" ]; then
    "$PGBIN/initdb" -D "$HOME/pgdata" -U elevator --auth=trust -E UTF8
    printf "listen_addresses = 'localhost'\nport = 5432\nunix_socket_directories = '/tmp'\n" \
      >> "$HOME/pgdata/postgresql.conf"
  fi
  "$PGBIN/pg_ctl" -D "$HOME/pgdata" -l "$HOME/pgdata/server.log" -w start
  "$PGBIN/psql" -h /tmp -U elevator -tc "SELECT 1 FROM pg_database WHERE datname='elevator_lab'" \
    | grep -q 1 || "$PGBIN/createdb" -h /tmp -U elevator elevator_lab
fi

# 2) 建表（SQLAlchemy create_all）
ROOT="$ROOT" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["ROOT"], "backend"))
from app.database import init_db
init_db()
print("database schema ready")
PY

# 3) 后端
( cd "$ROOT/backend" && nohup python3 -m uvicorn app.main:app \
    --host 127.0.0.1 --port 8000 --app-dir "$ROOT/backend" \
    > /tmp/elevator_api.log 2>&1 & echo "API  PID $!" )

# 4) 前端
( cd "$ROOT/frontend" && nohup ./node_modules/.bin/vite --host 127.0.0.1 \
    > /tmp/elevator_web.log 2>&1 & echo "WEB  PID $!" )

sleep 3
echo "API:  http://127.0.0.1:8000/docs"
echo "WEB:  http://127.0.0.1:5173"
