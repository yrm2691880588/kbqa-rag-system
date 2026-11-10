#!/usr/bin/env bash
# ============================================================
# HTTPS 证书申请脚本（Let's Encrypt 免费证书）
# ------------------------------------------------------------
# 用法：
#   bash deploy/init-letsencrypt.sh 你的域名 你的邮箱
#   例：bash deploy/init-letsencrypt.sh kbqa.example.com me@example.com
#
# 前置条件（缺一不可）：
#   1. 域名已解析到这台服务器（在域名控制台加一条 A 记录指向服务器 IP）
#   2. 前端容器正在运行（它提供证书校验地址），即已执行过 install.sh
#   3. 服务器的 80 端口对公网开放（安全组/防火墙放行）
#
# 说明：本脚本只"申请"证书；申请成功后还需按 deploy/README.md 的步骤切换到 HTTPS 配置。
# ============================================================

set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"

if [ -z "${DOMAIN}" ] || [ -z "${EMAIL}" ]; then
    echo "用法：bash deploy/init-letsencrypt.sh 你的域名 你的邮箱"
    echo "示例：bash deploy/init-letsencrypt.sh kbqa.example.com me@example.com"
    exit 1
fi

log() { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "[错误] $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "找不到 docker 命令"
docker inspect kbqa-frontend >/dev/null 2>&1 || fail "前端容器未运行，请先执行 bash deploy/install.sh"

log "检查域名解析是否已指向本机…"
SERVER_IP="$(curl -s --max-time 5 https://api.ipify.org || echo '')"
DOMAIN_IP="$(getent hosts "${DOMAIN}" | awk '{print $1}' | head -1 || echo '')"
log "  本机公网 IP : ${SERVER_IP:-未知}"
log "  域名解析 IP : ${DOMAIN_IP:-未解析}"
if [ -n "${SERVER_IP}" ] && [ -n "${DOMAIN_IP}" ] && [ "${SERVER_IP}" != "${DOMAIN_IP}" ]; then
    echo "⚠️  警告：域名解析的 IP 与本机公网 IP 不一致，证书申请很可能失败。"
    echo "    请先在域名控制台把 ${DOMAIN} 解析到 ${SERVER_IP}，等待生效后重试。"
    read -r -p "仍要继续尝试申请吗？(y/N) " GO
    [ "${GO}" = "y" ] || exit 0
fi

log "开始申请证书（域名：${DOMAIN}）…"
docker run --rm \
    -v kbqa_certbot_www:/var/www/certbot \
    -v kbqa_letsencrypt:/etc/letsencrypt \
    certbot/certbot certonly \
        --webroot -w /var/www/certbot \
        -d "${DOMAIN}" \
        --email "${EMAIL}" \
        --agree-tos --no-eff-email \
        --non-interactive

log "让 nginx 重新加载配置…"
docker exec kbqa-frontend nginx -s reload || true

echo ""
log "证书申请成功 ✅"
echo "   证书位置（容器内）：/etc/letsencrypt/live/${DOMAIN}/"
echo ""
echo "   接下来（三步切换到 HTTPS）："
echo "     1) 把 deploy/nginx-https.conf.example 里的 <你的域名> 换成 ${DOMAIN}，"
echo "        然后覆盖 frontend/nginx.conf"
echo "     2) 在 docker-compose.yml 的 frontend 端口映射里加上 \"443:443\""
echo "     3) docker compose --profile prod up -d --build frontend"
echo ""
echo "   证书自动续期（Let's Encrypt 证书 90 天到期，建议加一条定时任务）："
echo "     crontab -e 里加："
echo "     0 3 * * * cd /opt/kbqa && docker run --rm -v kbqa_certbot_www:/var/www/certbot -v kbqa_letsencrypt:/etc/letsencrypt certbot/certbot renew --webroot -w /var/www/certbot --quiet && docker exec kbqa-frontend nginx -s reload"
