#!/usr/bin/env bash
# tradview 启停脚本。
#
# 用 PID 文件管理进程，不要用 pkill -f 匹配命令行：这个项目的启动命令里含
# "uvicorn"/"vite" 字样，pkill -f 会把执行它的那个 shell 自己也匹配上并杀掉。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT/.run"
API_PORT="${TRADVIEW_PORT:-18090}"
WEB_PORT="${TRADVIEW_WEB_PORT:-18200}"
mkdir -p "$RUN_DIR"

pid_file() { echo "$RUN_DIR/$1.pid"; }
log_file() { echo "$RUN_DIR/$1.log"; }

is_alive() {
  local f; f="$(pid_file "$1")"
  [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null
}

start_api() {
  if is_alive api; then echo "api 已在运行 (pid $(cat "$(pid_file api)"))"; return; fi
  cd "$ROOT"
  setsid nohup "$ROOT/backend/.venv/bin/python" -m uvicorn app.main:app \
    --app-dir "$ROOT/backend" --host 127.0.0.1 --port "$API_PORT" \
    > "$(log_file api)" 2>&1 &
  echo $! > "$(pid_file api)"
  echo "api 启动中 (pid $!) -> http://127.0.0.1:$API_PORT"
}

start_web() {
  if is_alive web; then echo "web 已在运行 (pid $(cat "$(pid_file web)"))"; return; fi
  cd "$ROOT/frontend"
  setsid nohup npm run dev > "$(log_file web)" 2>&1 &
  echo $! > "$(pid_file web)"
  echo "web 启动中 (pid $!) -> http://127.0.0.1:$WEB_PORT"
}

stop_one() {
  local name="$1" f; f="$(pid_file "$name")"
  if [[ ! -f "$f" ]]; then echo "$name 未运行"; return; fi
  local pid; pid="$(cat "$f")"
  if kill -0 "$pid" 2>/dev/null; then
    # setsid 启动，pid 即进程组长：负号一次性收掉整棵树
    # （npm run dev 会 fork 出 vite 孙子进程，只 kill 父进程会漏）
    kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
    kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    echo "$name 已停止 (pid $pid)"
  else
    echo "$name 进程已不存在，清理 pid 文件"
  fi
  rm -f "$f"
}

status_one() {
  if is_alive "$1"; then echo "  $1: 运行中 (pid $(cat "$(pid_file "$1")"))"
  else echo "  $1: 未运行"; fi
}

case "${1:-}" in
  start)    start_api; start_web ;;
  start-api) start_api ;;
  stop)     stop_one web; stop_one api ;;
  restart)  stop_one web; stop_one api; sleep 1; start_api; start_web ;;
  restart-api) stop_one api; sleep 1; start_api ;;
  status)   echo "tradview:"; status_one api; status_one web ;;
  logs)     tail -n "${2:-40}" -F "$(log_file api)" ;;
  *)
    echo "用法: $0 {start|start-api|stop|restart|restart-api|status|logs [行数]}"
    exit 1 ;;
esac
