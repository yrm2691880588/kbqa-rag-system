#!/usr/bin/env bash
# ============================================================
# 重新打包 KBQA 镜像（Task 1）
# ------------------------------------------------------------
# 干什么：
#   1) 检查 Docker 引擎是否在跑（没跑就直接告诉你，不硬试）
#   2) 用国内镜像站把三个基础镜像拉下来，再打回标准名
#      —— 因为这台机器访问 Docker 官方仓库不通（已在 deploy/README.md 记录）
#   3) 用 compose 构建后端 + 前端两个镜像
#   4) 打上版本标签（带日期，便于回滚到上一个版本）
#   5) 打印构建结果，并把「镜像是否真的包含本次改动」的验证交给下一个脚本
#
# 用法（在项目根目录执行）：
#   bash scripts/build-images.sh
#
# ⚠️ 三个必须知道的坑（都是本项目实际踩过的）：
#   · `docker compose build` **不要接 | tail** —— 管道缓冲会让你完全看不到进度，
#     误以为“卡死了”。所以这里把输出重定向到日志文件，想看时再单独读。
#   · 构建慢的真凶是国外软件源，不是网络断。国内源已在根 .env 里配好。
#   · 构建“成功”不等于镜像里是新代码。构建后**必须**跑
#     scripts/check-image-freshness.sh 验证（历史上出现过旧构建把新镜像覆盖掉）。
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG_DIR="$ROOT/build-logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "============================================================"
echo "KBQA 镜像重新打包"
echo "============================================================"
echo "代码版本：$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '非 git 仓库')@$(git rev-parse --short HEAD 2>/dev/null || echo '-')"
echo "未提交改动：$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ') 个文件（本项目约定改动不提交，属正常）"
echo

# ---------- 1) 引擎检查 ----------
echo "[1/5] 检查 Docker 引擎…"
if ! docker info >/dev/null 2>&1; then
  cat <<'EOF'
  ❌ Docker 引擎没有运行，无法构建。

     请先手动启动 Docker Desktop（双击桌面图标 / 开始菜单里的 Docker Desktop）。
     注意：**必须由你自己启动**。我这边启动时会被系统的程序安全策略拦下
     （Docker Desktop 启动要调用 wsl.exe，而它在你的 Command Security 程序黑名单里）。

     启动后等托盘图标变成“引擎已启动”，再重新运行本脚本即可。
EOF
  exit 1
fi
echo "  ✅ 引擎正常：$(docker version --format '{{.Server.Version}}')"

# ---------- 2) 基础镜像：经镜像站拉取 ----------
# 直接拉 python:3.12-slim 会超时（Docker 官方仓库在国内访问不通）。
# 做法：从镜像站拉，然后 docker tag 改回标准名 —— 不需要改系统配置。
MIRROR="${DOCKER_MIRROR:-docker.1ms.run/library}"
BASE_IMAGES=(python:3.12-slim node:20-alpine nginx:1.27-alpine)

echo
echo "[2/5] 准备基础镜像（经镜像站 ${MIRROR}）…"
for img in "${BASE_IMAGES[@]}"; do
  if docker image inspect "$img" >/dev/null 2>&1; then
    echo "  ✅ 本地已有 $img"
    continue
  fi
  echo "  ⬇️  拉取 ${MIRROR}/${img} …"
  if docker pull "${MIRROR}/${img}" >"$LOG_DIR/pull-$(echo "$img" | tr ':/' '__').log" 2>&1; then
    docker tag "${MIRROR}/${img}" "$img"
    echo "  ✅ 已拉取并改名为 $img"
  else
    echo "  ⚠️  镜像站拉取失败，回退尝试官方源 $img …"
    if docker pull "$img" >/dev/null 2>&1; then
      echo "  ✅ 官方源可用，已获取 $img"
    else
      echo "  ❌ $img 拉取失败。可换一个镜像站后重试："
      echo "     DOCKER_MIRROR=docker.m.daocloud.io/library bash scripts/build-images.sh"
      exit 1
    fi
  fi
done

# ---------- 3) 构建 ----------
echo
echo "[3/5] 开始构建（后端 + 前端）…"
echo "  软件源：$(grep -E '^PIP_INDEX_URL=' .env 2>/dev/null | cut -d= -f2- || echo '（未配置，会走官方源，较慢）')"
echo "  日志：$LOG_DIR/build-$STAMP.log"
echo "  ⏱️  首次构建约 5~8 分钟（走国内源）；中途没有输出是正常的，不是卡死。"

BUILD_LOG="$LOG_DIR/build-$STAMP.log"
if docker compose --profile prod build --progress plain >"$BUILD_LOG" 2>&1; then
  echo "  ✅ 构建完成"
else
  echo "  ❌ 构建失败。最后 30 行日志："
  tail -30 "$BUILD_LOG" | sed 's/^/     /'
  echo "  完整日志见：$BUILD_LOG"
  exit 1
fi

# ---------- 4) 打版本标签 ----------
echo
echo "[4/5] 打版本标签…"
for name in kbqa-backend kbqa-frontend; do
  if docker image inspect "${name}:latest" >/dev/null 2>&1; then
    docker tag "${name}:latest" "${name}:${STAMP}"
    echo "  ✅ ${name}:latest → 同时标记为 ${name}:${STAMP}"
  else
    echo "  ❌ 找不到 ${name}:latest，构建可能没成功"
    exit 1
  fi
done

# ---------- 5) 结果 ----------
echo
echo "[5/5] 当前镜像："
docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedSince}}' \
  | grep kbqa || true

echo
echo "============================================================"
echo "构建完成。版本标签：$STAMP"
echo "============================================================"
cat <<EOF

下一步（**必做**）：验证镜像里确实是本次的新代码：

    bash scripts/check-image-freshness.sh

（历史上出现过旧构建把新镜像覆盖掉，导致“打包成功但跑的是旧代码”，
  而且不报任何错。所以这一步不能省。）

启动整套服务：

    docker compose --profile prod up -d

回滚到本次构建前的版本：把上一条命令里的镜像换成上一次的时间标签即可。
EOF
