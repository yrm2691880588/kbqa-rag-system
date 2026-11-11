"""应用入口：FastAPI 实例、跨域、路由挂载、启动初始化。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging_setup import setup_logging
from app.core.middleware import RequestContextMiddleware
from app.core.startup_check import run_startup_checks
from app.services import ai_guard, worker
from app.db.init_db import init_db
from app.db.session import engine
from app.services.vector_store import close_client, ensure_collection

# 日志配置：级别、格式（text/json）、脱敏、请求编号都由配置决定。
# 紧跟配置初始化执行，保证"服务还没起来时"的日志也是这个格式。
setup_logging(level=settings.log_level, fmt=settings.log_format)
logger = logging.getLogger("kbqa.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化数据库/管理员/向量库集合；关闭时释放连接池。"""
    # 第一道关：配置自检（生产环境关键配置缺失会直接拒绝启动）
    run_startup_checks()
    # 把配置里的参数应用到 AI 治理组件（限流/熔断/问题缓存）。
    # 为什么必须在启动时做一次：这些组件的默认值是"全关"（安全默认），
    # 不主动读配置的话，客户在 .env 里配了限流也不会生效 —— 这种"配了不生效"最难查。
    ai_guard.reload_from_settings()
    logger.info(
        "AI 治理已就绪：限流=%s/分钟，熔断=%s（连续失败 %d 次跳闸，冷却 %.0f 秒），问题缓存=%s（TTL %d 秒）",
        ai_guard.rate_limiter.per_minute or "不限",
        "开" if ai_guard.llm_breaker.enabled else "关",
        ai_guard.llm_breaker.fail_threshold,
        ai_guard.llm_breaker.open_seconds,
        "开" if ai_guard.question_cache.enabled else "关",
        ai_guard.question_cache.ttl_seconds,
    )
    logger.info("正在初始化数据库…")
    await init_db()
    # 确保 Qdrant 集合存在（幂等：已存在则跳过）
    try:
        await ensure_collection()
    except Exception as e:
        logger.warning("Qdrant 集合初始化失败（如向量库未启动会在此提示）：%s", e)
    # 拉起解析队列的工作进程：它负责把文档真正解析入库。
    # 放在这里而不是"上传时起一个任务"，是为了让解析工作能跨进程重启存活。
    worker.start_worker()
    logger.info("服务启动完成，接口前缀：%s", settings.api_prefix)
    yield
    await worker.stop_worker()
    await engine.dispose()
    await close_client()
    logger.info("服务已关闭")


app = FastAPI(
    title=settings.app_name,
    description="电商知识库 RAG 问答系统",
    version="0.1.0",
    lifespan=lifespan,
    # 接口文档开关：/docs 与 /redoc 是否对外暴露（EXPOSE_DOCS=false 可关闭，减少信息暴露）
    docs_url="/docs" if settings.expose_docs else None,
    redoc_url="/redoc" if settings.expose_docs else None,
    openapi_url="/openapi.json" if settings.expose_docs else None,
)

# 跨域：只允许配置里的前端开发地址访问
#
# ⚠️ 中间件顺序（Starlette 的 add_middleware 是 insert(0)，后加的在**外层**）：
#    最终执行顺序是「最后 add 的 -> 最先 add 的 -> 路由」。
#    所以这里**先加 CORS、后加请求追踪**，才能让请求追踪包在最外层：
#      请求进来 -> RequestContextMiddleware（发编号、记访问日志）
#              -> CORSMiddleware（可能在这里直接拒掉）
#              -> 路由
#    这样连"被 CORS 拦掉的请求"也会有编号和访问日志 ——
#    这类请求恰恰最需要排查：前端报"跨域失败"时，
#    你得能看出请求到底有没有打到后端。
#
#    早期版本顺序反了（先加追踪、后加 CORS），结果是
#    CORS 拒绝的请求完全没有日志，与下面注释想达到的效果正好相反。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 请求追踪：给每个请求一个编号，让"一次请求的所有日志"能串起来。
# 放在 CORS 之后 add，以便它位于最外层（见上方说明）。
app.add_middleware(RequestContextMiddleware)

# 挂载业务接口
app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/")
async def root():
    """根路径：给人看一眼服务基本信息。"""
    return {
        "app": settings.app_name,
        "env": settings.app_env,
        "docs": "/docs" if settings.expose_docs else None,
        "api_prefix": settings.api_prefix,
    }
