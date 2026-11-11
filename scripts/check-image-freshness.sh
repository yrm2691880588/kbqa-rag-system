#!/usr/bin/env bash
# ============================================================
# 验证「镜像里确实是本次的新代码」（Task 1 的验收环节）
# ------------------------------------------------------------
# 为什么必须有这一步：
#   构建“成功”、镜像存在、容器能起 —— 这三件事都成立，**仍然可能跑的是旧代码**。
#   本项目真实发生过：一次更早启动的旧构建在后台跑完，把新构建的镜像覆盖成了旧代码，
#   表面完全看不出来（构建成功、镜像在、容器正常起），只有跑一下才知道。
#
# 验证思路：
#   不看构建日志（那只能说明“打包动作执行了”），而是**进到镜像里问它自己**：
#   本次新增的模块、配置项、数据表、以及前端改名后的产物在不在。
#   每一条都对应本次改动的一个具体事实，旧镜像必然不满足。
#
# 用法（项目根目录）：
#   bash scripts/check-image-freshness.sh
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0
ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "============================================================"
echo "镜像新鲜度验证（确认镜像里是本次改动后的代码）"
echo "============================================================"

if ! docker info >/dev/null 2>&1; then
  echo "❌ Docker 引擎没在运行，先启动 Docker Desktop。"
  exit 1
fi

# ------------------------------------------------------------
# 后端：进镜像里问它自己
# ------------------------------------------------------------
echo
echo "[后端镜像 kbqa-backend:latest]"

# 这一坨检查用 python 在容器里跑。
# 注意：不传 DATABASE_URL（让它保持为空）是**故意的** ——
#       我们只检查“代码在不在”，不需要真连数据库；
#       而且这能顺便验证「配置缺失时会给人话提示」这个行为还在。
BACKEND_CHECK=$(cat <<'PYEOF'
import importlib, sys
sys.path.insert(0, "/app")
ok, bad = [], []

# 说明：下面这段检查里，有的模块（比如 worker）会在**导入时**就去建数据库连接池，
# 所以必须给一个数据库地址，否则 import 会抛
# "DATABASE_URL 未配置" —— 那是**检查脚本自己的问题**，不是镜像的问题
# （生产环境里这个变量一定是有值的）。
# 这里用 SQLite 临时库：够让模块导入成功，又完全不碰正式数据库。
# 注意：另外还有一条独立检查专门验证"不给数据库地址时会给人话提示"，那条不受影响。

# 1) 本次新增的服务模块是否都在（旧镜像里这些文件不存在）
for mod in ["app.services.ai_guard", "app.services.job_queue", "app.services.worker",
            "app.services.maintenance", "app.services.sms", "app.services.eval_metrics",
            "app.services.rerank", "app.core.logging_setup", "app.core.middleware"]:
    try:
        importlib.import_module(mod)
        ok.append(mod)
    except Exception as e:
        bad.append(f"import {mod} 失败: {type(e).__name__}: {str(e)[:120]}")

# 2) 新增的数据表是否已注册（解析任务表）
try:
    import app.models
    from app.db.base import Base
    tables = set(Base.metadata.tables)
    if "ingestion_jobs" in tables:
        ok.append(f"数据表 ingestion_jobs 已注册（共 {len(tables)} 张表）")
    else:
        bad.append("数据表 ingestion_jobs 不在模型里")
except Exception as e:
    bad.append(f"检查数据表失败: {type(e).__name__}")

# 3) 新增的配置项是否存在
try:
    from app.core.config import Settings
    fields = set(Settings.model_fields)
    need = ["ai_rate_limit_per_minute", "ai_cb_fail_threshold", "qa_cache_enabled",
            "db_pool_size", "web_concurrency", "ingestion_worker_enabled",
            "ingestion_max_attempts", "log_format", "log_retention_days",
            "rerank_api_key", "max_upload_mb"]
    missing = [n for n in need if n not in fields]
    if missing:
        bad.append("配置项缺失: " + ", ".join(missing))
    else:
        ok.append(f"11 个新增配置项齐全（配置项共 {len(fields)} 个）")
except Exception as e:
    bad.append(f"检查配置项失败: {type(e).__name__}")

# 4) 迁移工具是否装进去了
try:
    import alembic
    ok.append(f"alembic 已安装（{alembic.__version__}）")
except Exception:
    bad.append("alembic 没装进镜像")

# 5) 迁移脚本目录是否被拷进镜像
import os
if os.path.isdir("/app/migrations") or os.path.isdir("/migrations"):
    ok.append("迁移脚本目录已打进镜像")
else:
    bad.append("镜像里没有 migrations 目录（的迁移脚本没被打包）")
if os.path.isfile("/app/alembic.ini"):
    ok.append("alembic.ini 已打进镜像")
else:
    bad.append("镜像里没有 alembic.ini，迁移命令跑不了")

# 6) **敏感词库必须真的在镜像里，而且能加载出词**
#    这条专门防"静默失效"：.dockerignore 里有一条 `data/`，
#    而词库放在 app/data/ 下 —— 万一被连带排除，内容安全审核会**静默失效**
#    （不报错、不崩溃，只是永远不拦任何内容，极难发现）。
try:
    from app.services import content_filter
    wl = content_filter.load_wordlist(force=True)
    n = len(getattr(wl, "words", []) or [])
    if n > 0:
        ok.append(f"敏感词库已随镜像打包且可加载（{n} 个词）")
    else:
        bad.append("敏感词库为空 —— 内容安全会静默失效（多半是被 .dockerignore 误排除）")
except Exception as e:
    bad.append(f"加载敏感词库失败: {type(e).__name__}: {e}")

for line in ok:
    print("OK|" + line)
for line in bad:
    print("BAD|" + line)
PYEOF
)

BACK_OUT=$(docker run --rm -e DATABASE_URL="sqlite+aiosqlite:////tmp/chk.db" \
              --entrypoint python kbqa-backend:latest -c "$BACKEND_CHECK" 2>&1)
echo "$BACK_OUT" | grep '^OK|'  | sed 's/^OK|/  ✅ /' || true
echo "$BACK_OUT" | grep '^BAD|' | sed 's/^BAD|/  ❌ /' || true
PASS=$((PASS + $(echo "$BACK_OUT" | grep -c '^OK|')))
FAIL=$((FAIL + $(echo "$BACK_OUT" | grep -c '^BAD|')))

# 6) 配置缺失时应给人话提示（而不是抛一屏 SQLAlchemy 内部报错）
MSG=$(docker run --rm -e DATABASE_URL="" --entrypoint python kbqa-backend:latest \
        -c "import app.main" 2>&1 | tail -3)
if echo "$MSG" | grep -q "数据库连接地址"; then
  ok "缺配置时给出的是人话提示（不是 SQLAlchemy 内部报错）"
else
  bad "缺配置时没有给出人话提示，实际输出：$(echo "$MSG" | tail -1)"
fi

# ------------------------------------------------------------
# 后端：能不能真的启动起来（光有文件不够）
# ------------------------------------------------------------
# 做法：用 SQLite 临时库跑真实启动流程。
#   · 不用 PostgreSQL / Qdrant，所以**完全不碰你的正式数据**；
#   · 但启动流程、建表、注册模型、加载配置都是真的走的。
# 这能验证"镜像不只是文件对，而且真的能跑起来"。
echo
echo "[后端镜像：真实启动检查（SQLite 临时库，不碰正式数据）]"
BOOT_OUT=$(docker run --rm \
  -e DATABASE_URL="sqlite+aiosqlite:////tmp/boot_check.db" \
  -e APP_ENV=dev \
  --entrypoint python kbqa-backend:latest -c "
import asyncio, sys
sys.path.insert(0, '/app')
from app.db.init_db import init_db
from app.db.base import Base

async def main():
    await init_db()
    names = sorted(Base.metadata.tables)
    print('BOOT|建表成功，共 %d 张：%s' % (len(names), ', '.join(names[:5]) + ' …'))
    for must in ('ingestion_jobs', 'api_keys', 'usage_daily', 'org_quotas'):
        print(('BOOT|关键表存在：' if must in names else 'BOOTFAIL|缺表：') + must)

asyncio.run(main())
" 2>&1)
echo "$BOOT_OUT" | grep '^BOOT|'     | sed 's/^BOOT|/  ✅ /' || true
echo "$BOOT_OUT" | grep '^BOOTFAIL|' | sed 's/^BOOTFAIL|/  ❌ /' || true
PASS=$((PASS + $(echo "$BOOT_OUT" | grep -c '^BOOT|')))
FAIL=$((FAIL + $(echo "$BOOT_OUT" | grep -c '^BOOTFAIL|')))

# ------------------------------------------------------------
# 运维文件是否随镜像交付 + 容器内能否真的跑迁移
# ------------------------------------------------------------
# 这两条是本次验收**实际抓到的缺陷**：Dockerfile 原先只拷了 app/，
# 结果容器"能起能用"，但没有迁移脚本、没有运维脚本 ——
# 上线后想升级数据库结构、想清理旧数据，会发现命令根本不存在，
# 而且启动时不会有任何报错，只有真需要运维时才暴露。
echo
echo "[运维文件与迁移能力]"
FILES_OUT=$(docker run --rm --entrypoint sh kbqa-backend:latest -c '
for f in /app/alembic.ini /app/migrations/env.py /app/scripts/maintenance.py \
         /app/scripts/eval_retrieval.py /app/eval/retrieval_eval_set.example.json; do
  if [ -f "$f" ]; then echo "OK|$f"; else echo "MISS|$f"; fi
done')
echo "$FILES_OUT" | grep '^OK|'   | sed 's|^OK|/app/|' >/dev/null 2>&1 || true
echo "$FILES_OUT" | grep '^OK|'   | sed 's/^OK|/  ✅ 已在镜像内：/'
echo "$FILES_OUT" | grep '^MISS|' | sed 's/^MISS|/  ❌ 镜像内缺少：/'
PASS=$((PASS + $(echo "$FILES_OUT" | grep -c '^OK|')))
FAIL=$((FAIL + $(echo "$FILES_OUT" | grep -c '^MISS|')))

# 真在容器里跑一次迁移：能建出表才算"迁移工具真的可用"
# 说明：用一个**项目目录下的临时子目录**做挂载点（而不是系统临时目录），
#       这样清理一定成功、也不会有残留飘在系统临时文件夹里。
#       容器里没有 sqlite3 命令行工具，所以用"文件大小 + alembic 自己的输出"判断。
MIG_DIR="$ROOT/build-logs/mig-check"
rm -rf "$MIG_DIR" 2>/dev/null || true
mkdir -p "$MIG_DIR"
# ⚠️ 挂载源必须给 Docker 一个 **Windows 路径**（形如 E:\...）。
#    Git Bash 里的 "$ROOT" 是 /e/... 形式，直接传给 -v 时 Docker 会当成容器里的路径，
#    结果是"挂载了但里面对不上"，报的错是 "unable to open database file"，
#    完全看不出是路径格式问题。用 `pwd -W` 转成 Windows 路径即可。
HOST_MIG_DIR="$(cd "$MIG_DIR" && pwd -W 2>/dev/null || pwd)"
ALEMBIC_OUT=$(docker run --rm -v "$HOST_MIG_DIR:/mig" \
  -e DATABASE_URL="sqlite+aiosqlite:////mig/check.db" \
  --entrypoint sh kbqa-backend:latest -c \
  "cd /app && alembic upgrade head && alembic current" 2>&1)
if echo "$ALEMBIC_OUT" | grep -qi "Running upgrade"; then
  echo "  ✅ 容器内跑迁移成功（alembic 报告已执行升级）"
  PASS=$((PASS+1))
else
  echo "  ❌ 容器内跑 alembic 迁移失败，最后 8 行："
  echo "$ALEMBIC_OUT" | tail -8 | sed 's/^/     /'
  FAIL=$((FAIL+1))
fi
if [ -s "$MIG_DIR/check.db" ]; then
  echo "  ✅ 迁移确实建出了数据库文件（$(wc -c < "$MIG_DIR/check.db" | tr -d ' ') 字节）"
  PASS=$((PASS+1))
else
  echo "  ❌ 迁移没有产生数据库文件"
  FAIL=$((FAIL+1))
fi
rm -f "$MIG_DIR/check.db" 2>/dev/null || true
rmdir "$MIG_DIR" 2>/dev/null || true

# ------------------------------------------------------------
# 前端：构建产物里应包含改名后的页面
# ------------------------------------------------------------
echo
echo "[前端镜像 kbqa-frontend:latest]"

# 本次把 AdminPlaceholderView.vue 改名为 KnowledgeBaseView.vue。
# 所以新镜像的 JS 产物里应该出现 KnowledgeBaseView、且不再有 AdminPlaceholder。
ASSETS=$(docker run --rm --entrypoint sh kbqa-frontend:latest \
          -c "ls /usr/share/nginx/html/assets/ 2>/dev/null | tr '\n' ' '")
if [ -z "$ASSETS" ]; then
  bad "读不到前端静态产物（/usr/share/nginx/html/assets 为空）"
else
  if echo "$ASSETS" | grep -q "KnowledgeBaseView"; then
    ok "前端产物含改名后的页面（KnowledgeBaseView）"
  else
    bad "前端产物里没有 KnowledgeBaseView（镜像里是改名前的旧代码）"
  fi
  if echo "$ASSETS" | grep -q "AdminPlaceholder"; then
    bad "前端产物里仍有旧的 AdminPlaceholder（说明是改名前的构建）"
  else
    ok "前端产物里已无旧页面名"
  fi
fi

# nginx 配置里的大文件上传放行是否在
if docker run --rm --entrypoint sh kbqa-frontend:latest \
     -c "grep -q 'client_max_body_size' /etc/nginx/conf.d/default.conf" 2>/dev/null; then
  ok "nginx 已放行大文件上传（client_max_body_size）"
else
  bad "nginx 配置里没有 client_max_body_size（大文件上传会被拦）"
fi

# ------------------------------------------------------------
echo
echo "============================================================"
echo "结果：通过 $PASS 项，失败 $FAIL 项"
echo "============================================================"
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 镜像里不是最新代码 —— 不要用它部署，请重新构建。"
  exit 1
fi
echo "✅ 镜像确认为本次改动后的代码，可以部署。"
exit 0
