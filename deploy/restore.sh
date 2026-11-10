#!/usr/bin/env bash
# ============================================================
# 数据恢复脚本（在服务器上执行）
# ------------------------------------------------------------
# 用法：
#   bash deploy/restore.sh                      # 恢复"最新的一份"备份
#   bash deploy/restore.sh backups/20261008-031500   # 恢复指定备份
#
# ⚠️ 这是覆盖性操作：数据库与向量数据会被替换成备份里的版本。
#    脚本会先打印备份清单，并要求你手动输入 yes 才会继续。
# ============================================================

set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-$(pwd)/backups}"
PG_CONTAINER="kbqa-postgres"
PG_USER="${POSTGRES_USER:-kbqa}"
PG_DB="${POSTGRES_DB:-kbqa}"
QDRANT_VOLUME="kbqa_qdrant_data"
UPLOADS_VOLUME="kbqa_uploads_data"
TAR_IMAGE="alpine:3.20"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "[错误] $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "找不到 docker 命令"

# ---------- 1. 定位要恢复的备份 ----------
BACKUP_DIR="${1:-}"
if [ -z "${BACKUP_DIR}" ]; then
    BACKUP_DIR="$(find "${BACKUP_ROOT}" -maxdepth 1 -type d -regextype posix-extended \
        -regex '.*/[0-9]{8}-[0-9]{6}' 2>/dev/null | sort -r | head -1)"
    [ -n "${BACKUP_DIR}" ] || fail "在 ${BACKUP_ROOT} 下没有找到任何备份"
    log "未指定备份，自动使用最新一份：${BACKUP_DIR}"
fi

[ -d "${BACKUP_DIR}" ] || fail "备份目录不存在：${BACKUP_DIR}"
[ -s "${BACKUP_DIR}/database.dump" ] || fail "备份不完整：缺少 database.dump"
[ -s "${BACKUP_DIR}/qdrant_volume.tar.gz" ] || fail "备份不完整：缺少 qdrant_volume.tar.gz"
[ -s "${BACKUP_DIR}/uploads_volume.tar.gz" ] || fail "备份不完整：缺少 uploads_volume.tar.gz"

# ---------- 2. 打印清单并要求确认 ----------
echo ""
echo "将要恢复的备份内容："
echo "--------------------------------------------------------------"
if [ -f "${BACKUP_DIR}/manifest.txt" ]; then cat "${BACKUP_DIR}/manifest.txt"; fi
du -h "${BACKUP_DIR}"/* | sed 's/^/  /'
echo "--------------------------------------------------------------"
echo "⚠️  此操作会用备份内容覆盖当前数据库、向量库与上传文件！"
read -r -p "确认恢复请输入 yes（其他任何输入都会取消）： " ANSWER
[ "${ANSWER}" = "yes" ] || { echo "已取消，未做任何改动。"; exit 0; }

# ---------- 3. 停止写入方 ----------
log "停止应用与向量库（避免恢复过程中有写入）…"
docker stop kbqa-frontend kbqa-backend kbqa-qdrant >/dev/null 2>&1 || true

# ---------- 4. 恢复数据库 ----------
log "① 恢复数据库…"
docker exec -i "${PG_CONTAINER}" pg_restore -U "${PG_USER}" -d "${PG_DB}" \
    --clean --if-exists --no-owner < "${BACKUP_DIR}/database.dump" || {
        fail "数据库恢复失败（若提示大量 warning 属正常，出现 error 才算失败）"
    }

# ---------- 5. 恢复向量数据 ----------
log "② 恢复向量数据…"
# 先清空数据卷内容，再解包（避免新旧数据混杂）
docker run --rm -v "${QDRANT_VOLUME}":/data "${TAR_IMAGE}" \
    sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null || true'
docker run --rm \
    -v "${QDRANT_VOLUME}":/data \
    -v "${BACKUP_DIR}":/backup:ro \
    "${TAR_IMAGE}" tar xzf /backup/qdrant_volume.tar.gz -C /data

# ---------- 6. 恢复上传文件 ----------
log "③ 恢复上传文件…"
docker run --rm -v "${UPLOADS_VOLUME}":/data "${TAR_IMAGE}" \
    sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null || true'
docker run --rm \
    -v "${UPLOADS_VOLUME}":/data \
    -v "${BACKUP_DIR}":/backup:ro \
    "${TAR_IMAGE}" tar xzf /backup/uploads_volume.tar.gz -C /data

# ---------- 7. 重新启动 ----------
log "④ 重新启动服务…"
docker compose --profile prod up -d

echo ""
log "恢复完成 ✅"
echo "   请验证："
echo "     1) 浏览器打开站点，用原账号登录"
echo "     2) 打开知识库，确认文档与片段数正常"
echo "     3) 提一个已知问题，确认能检索到资料并回答"
