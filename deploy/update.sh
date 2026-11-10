#!/usr/bin/env bash
# ============================================================
# 升级脚本（把服务器上的服务更新到最新代码）
# ------------------------------------------------------------
# 用法：bash deploy/update.sh
#
# 执行顺序（每一步都是为了让"升级出问题时能退回去"）：
#   ① 先备份数据（升级不改数据结构？也可能出错，先留后路）
#   ② 把当前镜像标记为 previous（回滚时用）
#   ③ 拉取最新代码
#   ④ 重新构建并滚动重启
#   ⑤ 等待健康检查；不通过则提示可用哪个命令回滚
# ============================================================

set -euo pipefail

COMPOSE=""
log() { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "[错误] $*" >&2; exit 1; }

detect_compose() {
    if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"
    else fail "未找到 docker compose"
    fi
}

detect_compose

# ---------- ① 备份 ----------
log "① 升级前备份数据…"
if [ -f deploy/backup.sh ]; then
    bash deploy/backup.sh || fail "备份失败，已中止升级（避免出问题时无法恢复）"
else
    log "   未找到备份脚本，跳过备份"
fi

# ---------- ② 保存当前镜像 ----------
log "② 保存当前版本镜像（供回滚）…"
docker tag kbqa-backend:latest kbqa-backend:previous 2>/dev/null || log "   首次升级，无旧后端镜像"
docker tag kbqa-frontend:latest kbqa-frontend:previous 2>/dev/null || log "   首次升级，无旧前端镜像"

# ---------- ③ 拉取代码 ----------
if [ -d .git ]; then
    log "③ 拉取最新代码…"
    git pull --ff-only || fail "代码拉取失败（本地有未提交改动？请先处理）"
else
    log "③ 非 git 目录，跳过代码拉取（请确认你已手动更新文件）"
fi

# ---------- ④ 重建并启动 ----------
log "④ 重新构建并启动…"
${COMPOSE} --profile prod up -d --build

# ---------- ⑤ 健康检查 ----------
log "⑤ 等待服务就绪…"
READY=0
for i in $(seq 1 60); do
    if docker exec kbqa-backend python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready',timeout=3)" >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 3
done

echo ""
if [ "${READY}" = "1" ]; then
    log "升级完成 ✅"
    echo "   查看日志：${COMPOSE} logs -f backend"
else
    log "升级后健康检查未通过 ⚠️"
    echo "   排查看日志：${COMPOSE} logs --tail=100 backend"
    echo "   要退回上一个版本，执行：bash deploy/rollback.sh"
    exit 1
fi
