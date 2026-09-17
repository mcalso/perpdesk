#!/usr/bin/env bash
# 把代码部署到服务器。
#
#   ./scripts/deploy.sh                        # 服务器上拉 git 最新代码并重启
#   ./scripts/deploy.sh --local                # 改用本地工作区代码（含未提交改动）
#   ./scripts/deploy.sh --build-local          # 前端在本机构建后上传 dist
#   ./scripts/deploy.sh --local --build-local  # 两者可叠加
#   ./scripts/deploy.sh --force-install        # 强制重装依赖（没变也装）
#
# 依赖默认按哈希跳过：requirements.txt / package-lock.json 没变就不重装。
# 这不是优化而是必需 —— npm ci 会先删光 node_modules 再全量重装，而本项目
# 面向的是自托管小 VPS（实测某台上行只有 0.5 Mbps，重下 111M 依赖要半小时
# 以上），而绝大多数部署根本没动过依赖。
set -euo pipefail

HOST="${PERPDESK_HOST_SSH:-perpdesk}"
REMOTE_DIR=/opt/perpdesk
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYNC_LOCAL=0
BUILD_LOCAL=0
FORCE_INSTALL=0

# 把文件头的注释原样当帮助打出来，省得两处各写一份说明再各自过期
usage() { awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "${BASH_SOURCE[0]}"; }

for arg in "$@"; do
  case "$arg" in
    --local)         SYNC_LOCAL=1 ;;
    --build-local)   BUILD_LOCAL=1 ;;
    --force-install) FORCE_INSTALL=1 ;;
    -h|--help)       usage; exit 0 ;;
    *) printf '未知参数: %s\n\n' "$arg" >&2; usage >&2; exit 1 ;;
  esac
done

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

# ---------- 1. 同步代码 ----------
if (( SYNC_LOCAL )); then
  say "同步本地工作区到 $HOST:$REMOTE_DIR"
  # --no-owner --no-group：rsync 以 root 跑时 -a 会把本机的 uid/gid 原样盖到
  # 服务器上（实测把 /opt/perpdesk 变成 1024:1027），之后 git 判定 repo 属主
  # 可疑而拒绝工作，git 部署路径就此失效。
  #
  # --exclude '*.key'：.master.key 是凭据加密的主密钥，只存在于服务器上。
  # 少这一条，--delete 会把它删掉，库里所有 API 凭据当场变成解不开的密文。
  rsync -az --no-owner --no-group --delete \
    --exclude '.git' --exclude 'node_modules' --exclude '.venv' \
    --exclude 'data' --exclude '.run' --exclude 'backend/.env' \
    --exclude '*.key' \
    --exclude 'frontend/dist' \
    "$ROOT/" "$HOST:$REMOTE_DIR/"
else
  say "在服务器上拉取 git 最新代码"
  ssh "$HOST" "cd $REMOTE_DIR && git fetch -q origin && git reset -q --hard origin/main"
fi

# ---------- 2. 依赖与构建 ----------
# 戳记放在 data/ 下：那里既被 rsync 排除也被 git 忽略，
# 不会被 --delete 或 reset --hard 清掉。
REMOTE_PRELUDE=$(cat <<EOS
set -euo pipefail
cd $REMOTE_DIR
STATE=data/.deploy-state
mkdir -p "\$STATE"
FORCE=$FORCE_INSTALL
EOS
)

# shellcheck disable=SC2016
REMOTE_HELPERS='
dep_hash() { sha256sum "$1" | cut -d" " -f1; }
# 已装过且依赖文件没变 → 返回 0（可跳过）
stamp_ok() {
  (( FORCE )) && return 1
  [[ "$(cat "$STATE/$2" 2>/dev/null)" == "$(dep_hash "$1")" ]]
}
# 只在安装真的成功之后才落戳记：set -e 会让失败的安装直接中断，
# 戳记停在旧值，下次部署会重试
save_stamp() { dep_hash "$1" > "$STATE/$2"; }
'

REMOTE_BACKEND='
[[ -d backend/.venv ]] || python3 -m venv backend/.venv
if stamp_ok backend/requirements.txt requirements.sha; then
  echo "  后端依赖未变，跳过 pip install"
else
  echo "  后端依赖有变（或首次部署），安装中…"
  backend/.venv/bin/pip install -q --upgrade pip
  backend/.venv/bin/pip install -q -r backend/requirements.txt
  save_stamp backend/requirements.txt requirements.sha
fi
'

REMOTE_FRONTEND='
if [[ -d frontend/node_modules ]] && stamp_ok frontend/package-lock.json package-lock.sha; then
  echo "  前端依赖未变，跳过 npm ci"
else
  echo "  前端依赖有变（或首次部署），安装中…"
  ( cd frontend && { npm ci --no-audit --no-fund 2>&1 | tail -2 \
                     || npm install --no-audit --no-fund 2>&1 | tail -2; } )
  save_stamp frontend/package-lock.json package-lock.sha
fi
echo "  构建前端…"
( cd frontend && npm run build 2>&1 | tail -3 )
'

remote() { printf '%s\n' "$REMOTE_PRELUDE" "$REMOTE_HELPERS" "$@" | ssh "$HOST" "bash -s"; }

if (( BUILD_LOCAL )); then
  say "在本机构建前端（服务器内存跑不动 vite 时用它）"
  ( cd "$ROOT/frontend" && npm run build 2>&1 | tail -3 )
  say "上传 dist"
  rsync -az --no-owner --no-group --delete \
    "$ROOT/frontend/dist/" "$HOST:$REMOTE_DIR/frontend/dist/"
  say "安装后端依赖"
  remote "$REMOTE_BACKEND"
else
  say "安装依赖并构建"
  remote "$REMOTE_BACKEND" "$REMOTE_FRONTEND"
fi

# ---------- 3. 重启与验收 ----------
say "重启服务"
ssh "$HOST" "systemctl restart perpdesk && sleep 3 && systemctl is-active perpdesk"

say "健康检查"
ssh "$HOST" "curl -s localhost:18090/api/health" | python3 -m json.tool
