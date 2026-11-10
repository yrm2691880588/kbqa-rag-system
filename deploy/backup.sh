#!/usr/bin/env bash
# ============================================================
# 数据备份脚本（在服务器上执行）
# ------------------------------------------------------------
# 备份哪三份数据：
#   1. PostgreSQL  —— 用户、会话、消息、知识库元数据（用官方 pg_dump，事务一致）
#   2. Qdrant      —— 知识库片段的向量数据（打包数据卷）
#   3. 上传文件    —— 用户上传的原始文档（打包数据卷）
#
# 用法：
#   bash deploy/backup.sh                    # 备份到 ./backups/<时间戳>/
#   BACKUP_ROOT=/data/backups bash deploy/backup.sh   # 指定备份根目录
#   BACKUP_KEEP=14 bash deploy/backup.sh     # 保留最近 14 份（默认 7 份）
#
# 建议：配成每天凌晨自动执行（见 deploy/README.md 的定时任务一节）
# ============================================================

set -euo pipefail   # 出错立即停止；用到未定义变量报错；管道任一环节失败即失败

# ---------- 可调参数 ----------
BACKUP_ROOT="${BACKUP_ROOT:-$(pwd)/backups}"   # 备份存放根目录
BACKUP_KEEP="${BACKUP_KEEP:-7}"                # 保留最近几份
PG_CONTAINER="kbqa-postgres"
PG_USER="${POSTGRES_USER:-kbqa}"
PG_DB="${POSTGRES_DB:-kbqa}"
QDRANT_VOLUME="kbqa_qdrant_data"
UPLOADS_VOLUME="kbqa_uploads_data"
# 打包用的临时镜像（很小，首次会自动下载）
TAR_IMAGE="alpine:3.20"

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="${BACKUP_ROOT}/${TIMESTAMP}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "[错误] $*" >&2; exit 1; }

# ---------- 前置检查 ----------
command -v docker >/dev/null 2>&1 || fail "找不到 docker 命令，请在装有 Docker 的服务器上执行"
docker inspect "${PG_CONTAINER}" >/dev/null 2>&1 \
    || fail "找不到数据库容器 ${PG_CONTAINER}，请确认服务已启动（docker compose --profile prod up -d）"

for vol in "${QDRANT_VOLUME}" "${UPLOADS_VOLUME}"; do
    docker volume inspect "${vol}" >/dev/null 2>&1 || fail "找不到数据卷 ${vol}，请确认服务已启动过至少一次"
done

mkdir -p "${TARGET}"
log "开始备份 -> ${TARGET}"

# ---------- 1. PostgreSQL ----------
log "① 备份数据库（pg_dump 逻辑备份，保证一致性）…"
docker exec "${PG_CONTAINER}" pg_dump -U "${PG_USER}" -d "${PG_DB}" -F c > "${TARGET}/database.dump"
[ -s "${TARGET}/database.dump" ] || fail "数据库备份文件为空，备份中止"

# ---------- 2. Qdrant 向量数据 ----------
# 说明：直接打包数据卷，简单可靠且与版本无关；建议在访问低峰执行以减少写入差异
log "② 备份向量数据（打包数据卷）…"
docker run --rm \
    -v "${QDRANT_VOLUME}":/data:ro \
    -v "${TARGET}":/backup \
    "${TAR_IMAGE}" tar czf /backup/qdrant_volume.tar.gz -C /data .

# ---------- 3. 上传文件 ----------
log "③ 备份上传文件（打包数据卷）…"
docker run --rm \
    -v "${UPLOADS_VOLUME}":/data:ro \
    -v "${TARGET}":/backup \
    "${TAR_IMAGE}" tar czf /backup/uploads_volume.tar.gz -C /data .

# ---------- 4. 生成校验清单 ----------
log "④ 生成校验清单…"
MANIFEST="${TARGET}/manifest.txt"
{
    echo "备份时间: ${TIMESTAMP}"
    echo "主机名  : $(hostname)"
    echo "Docker  : $(docker --version)"
    echo "---- 文件清单（含 SHA256 校验和）----"
    for f in database.dump qdrant_volume.tar.gz uploads_volume.tar.gz; do
        size="$(du -h "${TARGET}/${f}" | cut -f1)"
        sum="$(sha256sum "${TARGET}/${f}" | cut -d' ' -f1)"
        echo "${f}  大小=${size}  SHA256=${sum}"
    done
} > "${MANIFEST}"

# ---------- 5. 清理旧备份（只删本目录下符合时间戳命名的备份夹）----------
log "⑤ 清理旧备份（保留最近 ${BACKUP_KEEP} 份）…"
# 只匹配形如 20261008-031500 的目录名，避免误删其他文件
find "${BACKUP_ROOT}" -maxdepth 1 -type d -regextype posix-extended \
     -regex '.*/[0-9]{8}-[0-9]{6}' 2>/dev/null | sort -r | tail -n +"$((BACKUP_KEEP + 1))" \
     | while read -r old; do
           log "   删除过期备份：$(basename "${old}")"
           rm -rf "${old}"
       done || true

# ---------- 完成摘要 ----------
echo ""
log "备份完成 ✅"
echo "   备份目录：${TARGET}"
echo "   文件清单："
du -h "${TARGET}"/* | sed 's/^/     /'
echo ""
echo "   恢复到本机：bash deploy/restore.sh ${TARGET}"
echo "   建议把备份目录同步到另一台机器或对象存储（本机磁盘坏了备份也会一起没）"
