#!/usr/bin/env bash
# ============================================================
# 一键安装脚本（首次部署时在服务器上执行）
# ------------------------------------------------------------
# 用法：
#   1. 把项目放到服务器上，例如：git clone <仓库地址> /opt/kbqa && cd /opt/kbqa
#   2. bash deploy/install.sh
#
# 脚本会做：
#   ① 检查服务器环境（Docker 是否就绪）
#   ② 生成配置文件模板并自动生成随机密钥
#   ③ 检查配置是否填完整（有占位符就提示你先填）
#   ④ 构建镜像、启动全部服务
#   ⑤ 等待服务就绪并打印访问地址
# ============================================================

set -euo pipefail

COMPOSE=""
log() { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "[错误] $*" >&2; exit 1; }

detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE="docker-compose"
    else
        fail "未找到 docker compose，请先安装 Docker（推荐 Docker Engine 官方安装脚本）"
    fi
}

# ---------- ① 环境检查 ----------
log "① 检查运行环境…"
command -v docker >/dev/null 2>&1 || fail "未安装 Docker"
docker info >/dev/null 2>&1 || fail "Docker 未运行，请先启动：systemctl start docker"
detect_compose
log "   Docker 就绪，compose 命令：${COMPOSE}"

# ---------- ② 准备配置文件 ----------
log "② 准备配置文件…"

# 数据库口令文件（容器编排读取）
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        log "   已从 .env.example 生成根目录 .env（数据库账号口令）"
    else
        fail "缺少 .env.example，无法生成数据库配置文件"
    fi
fi

# 后端生产配置（首次从模板生成，并自动生成随机签名密钥）
if [ ! -f backend/.env ]; then
    [ -f backend/.env.prod.example ] || fail "缺少 backend/.env.prod.example 模板"
    cp backend/.env.prod.example backend/.env
    # 自动生成一个强随机密钥（避免用户不会生成）
    if command -v openssl >/dev/null 2>&1; then
        SECRET="$(openssl rand -base64 48 | tr -d '\n')"
    else
        SECRET="$(head -c 48 /dev/urandom | base64 | tr -d '\n')"
    fi
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" backend/.env
    log "   已生成 backend/.env，并自动写入随机签名密钥"
fi

# ---------- ③ 检查配置是否填完 ----------
PENDING_LINES="$(grep -nE '^[A-Z_]+=<' backend/.env | cut -d: -f1 | tr '\n' ' ' || true)"
if [ -n "${PENDING_LINES}" ]; then
    echo ""
    echo "⚠️  配置文件还没填完：backend/.env 的第 ${PENDING_LINES}行 仍是 <占位符>"
    echo "    请用编辑器打开 backend/.env，把下列项目填成真实值："
    grep -nE '^[A-Z_]+=<' backend/.env | sed -E 's/=.*/ = （待填写）/' | sed 's/^/      /'
    echo ""
    echo "    填好后重新运行：bash deploy/install.sh"
    exit 1
fi

# 数据库口令也不能是占位符
if grep -qE '<' .env 2>/dev/null; then
    echo ""
    echo "⚠️  根目录 .env 里还有 <占位符>，请填写数据库账号口令后重新运行"
    grep -nE '^[A-Z_]+=<' .env | sed -E 's/=.*/ = （待填写）/' | sed 's/^/      /'
    exit 1
fi

# ---------- ④ 构建并启动 ----------
log "④ 构建镜像并启动服务（首次构建需要几分钟）…"
# 构建失败时给出「可执行的下一步」，而不是让人对着超时日志干等。
# 最常见的失败原因：国内服务器连不上 Docker 官方仓库。
if ! ${COMPOSE} --profile prod up -d --build; then
    echo ""
    echo "❌ 构建或启动失败。"
    echo "   如果上面的日志里出现 'dial tcp ... timeout'、'no such host'，"
    echo "   或者长时间卡在 Pulling / Building 不动，基本可以确定是："
    echo "   国内网络访问 Docker 官方仓库不通（跟本项目代码无关）。"
    echo ""
    echo "   解决办法见 deploy/README.md 第一节「国内服务器必读：配置镜像加速」。"
    echo "   最快的一条路（不需要改系统配置）："
    echo "       MIRROR=docker.1ms.run/library"
    echo "       for img in python:3.12-slim node:20-alpine nginx:1.27-alpine alpine:3.20; do"
    echo "           docker pull \"\$MIRROR/\$img\" && docker tag \"\$MIRROR/\$img\" \"\$img\""
    echo "       done"
    echo "   然后重新执行：bash deploy/install.sh"
    exit 1
fi

# ---------- ⑤ 等待就绪 ----------
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
    SERVER_IP="$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || echo '服务器IP')"
    log "部署完成 ✅"
    echo "   访问地址：http://${SERVER_IP}/"
    echo "   查看日志：${COMPOSE} logs -f backend"
    echo "   健康检查：curl http://127.0.0.1/api/v1/health"
    echo ""
    echo "   下一步建议："
    echo "     1) 检查 .env 中的管理员密码是否足够强（首次启动会用它建管理员账号）"
    echo "     2) 配好域名后启用 HTTPS（见 deploy/README.md）"
    echo "     3) 配置每日自动备份（见 deploy/README.md）"
else
    log "服务已启动，但健康检查尚未通过（可能需要更长时间初始化）"
    echo "   请查看日志排查：${COMPOSE} logs --tail=80 backend"
    exit 1
fi
