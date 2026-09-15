#!/usr/bin/env bash
# 把当前代码部署到服务器。
#
#   ./scripts/deploy.sh            # 拉取 git 最新代码并重启
#   ./scripts/deploy.sh --local    # 用本地工作区代码（含未提交改动）
#
# 前端构建默认在服务器上做；服务器内存紧张时加 --build-local 在本机构建后上传 dist。
set -euo pipefail

HOST="${PERPDESK_HOST_SSH:-perpdesk}"
REMOTE_DIR=/opt/perpdesk
MODE="${1:-}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

if [[ "$MODE" == "--local" ]]; then
  say "同步本地工作区到 $HOST:$REMOTE_DIR"
  # --no-owner --no-group：rsync 以 root 跑时 -a 会把本机的 uid/gid 原样
  # 盖到服务器上（实测把 /opt/perpdesk 变成 1024:1027），之后 git 会判定
  # repo 属主可疑而拒绝工作，git 部署路径就此失效
  rsync -az --no-owner --no-group --delete \
    --exclude '.git' --exclude 'node_modules' --exclude '.venv' \
    --exclude 'data' --exclude '.run' --exclude 'backend/.env' \
    --exclude '*.key' \
    --exclude 'frontend/dist' \
    ./ "$HOST:$REMOTE_DIR/"
else
  say "在服务器上拉取 git 最新代码"
  ssh "$HOST" "cd $REMOTE_DIR && git fetch -q origin && git reset -q --hard origin/main"
fi

say "安装依赖并构建"
ssh "$HOST" "bash -s" <<'REMOTE'
set -euo pipefail
cd /opt/perpdesk
[[ -d backend/.venv ]] || python3 -m venv backend/.venv
backend/.venv/bin/pip install -q --upgrade pip
backend/.venv/bin/pip install -q -r backend/requirements.txt
cd frontend
npm ci --no-audit --no-fund 2>&1 | tail -2 || npm install --no-audit --no-fund 2>&1 | tail -2
npm run build 2>&1 | tail -3
REMOTE

say "重启服务"
ssh "$HOST" "systemctl restart perpdesk && sleep 3 && systemctl is-active perpdesk"

say "健康检查"
ssh "$HOST" "curl -s localhost:18090/api/health" | python3 -m json.tool
