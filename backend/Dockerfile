# ============================================================
# 后端镜像：FastAPI + uvicorn（多进程）
# ------------------------------------------------------------
# 设计要点：
#   1. 装字体包：图形验证码要把字符画到图片上，基础镜像没有字体会退化成
#      极小的点阵字，用户根本看不清验证码；
#   2. 依赖与代码分层拷贝：requirements 不变时重建镜像不会重装依赖（构建快很多）；
#   3. 以非 root 用户运行：万一应用被攻破，攻击者拿不到容器 root 权限；
#   4. 信任反向代理头：经过 nginx 后必须让后端拿到"真实客户端 IP"，
#      否则所有用户共用一个 IP，登录/注册限流会误伤全部人；
#   5. 存活探针只探进程：依赖（数据库）抖一下不该导致容器被反复重启。
# ============================================================
FROM python:3.12-slim

# 1) 系统依赖：字体（验证码渲染用）
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 2) 先装 Python 依赖（独立成层，利用构建缓存）
COPY requirements.txt ./
# 可选：指定 pip 源。部分网络环境下从官方源装依赖很慢，
# 可在构建时传入国内镜像（留空则用官方源），例如：
#   docker compose --profile prod build \
#     --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ backend
ARG PIP_INDEX_URL=""
RUN if [ -n "${PIP_INDEX_URL}" ]; then \
        pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

# 3) 再拷业务代码
#    ⚠️ 除了 app/，下面这几样**必须**一起拷进来，否则在服务器上没法运维：
#      · alembic.ini + migrations/ —— 数据库结构升级（alembic upgrade head）
#      · scripts/                 —— 数据清理与归档、检索评估等运维脚本
#      · eval/                    —— 检索评估集模板
#    曾经只拷了 app/，结果是"容器起来能用，但没法做数据库升级、没法清理数据"，
#    而这个缺陷不会在启动时报错，只有真正需要运维时才发现。
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY eval ./eval

# 4) 非 root 用户 + 上传目录（空数据卷会继承该目录属主，保证容器内可写）
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /app/data/uploads \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 5) 存活探针：只探进程是否响应（不检查依赖）
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live',timeout=3).status==200 else 1)"

# 6) 启动：多进程 + 信任反代头（容器端口不对公网开放，仅 nginx 能访问）
#    UVICORN_WORKERS 由编排文件注入，默认 4（注意：进程数 × 每进程连接池不要超过数据库上限 100）
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-4} --proxy-headers --forwarded-allow-ips='*'"]
