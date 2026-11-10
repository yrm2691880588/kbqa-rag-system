#!/usr/bin/env bash
# ============================================================
# 回滚脚本（升级出问题时，退回上一个版本）
# ------------------------------------------------------------
# 用法：bash deploy/rollback.sh
#
# 原理：update.sh 在升级前会把当时的镜像另存一份叫 previous，
#       本脚本把 previous 重新指回 latest 并重建容器。
# 注意：回滚代码不会回滚数据（数据由备份负责，见 deploy/restore.sh）。
#       若新版本改了数据库结构，回滚后可能需要配合恢复备份。
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

# ---------- 检查是否真的有上一版本 ----------
docker image inspect kbqa-backend:previous >/dev/null 2>&1 \
    || fail "没有找到上一个版本的镜像（可能从未执行过升级），无法回滚"

log "把上一版本镜像切换为当前版本…"
docker tag kbqa-backend:previous kbqa-backend:latest
docker image inspect kbqa-frontend:previous >/dev/null 2>&1 && docker tag kbqa-frontend:previous kbqa-frontend:latest || true

log "重建容器（使用上一版本镜像，不重新构建）…"
${COMPOSE} --profile prod up -d --no-build --force-recreate

log "等待服务就绪…"
READY=0
for i in $(seq 1 40); do
    if docker exec kbqa-backend python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live',timeout=3)" >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 3
done

echo ""
if [ "${READY}" = "1" ]; then
    log "回滚完成 ✅（当前运行的是上一个版本）"
    echo "   若数据也需回退，请用：bash deploy/restore.sh"
else
    fail "回滚后服务仍未就绪，请查看日志：${COMPOSE} logs --tail=100 backend"
fi
