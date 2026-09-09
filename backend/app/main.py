"""应用入口：FastAPI 实例、跨域、路由挂载、启动初始化。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.db.init_db import init_db
from app.db.session import engine
from app.services.vector_store import close_client, ensure_collection

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("kbqa.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化数据库/管理员/向量库集合；关闭时释放连接池。"""
    logger.info("正在初始化数据库…")
    await init_db()
    # 确保 Qdrant 集合存在（幂等：已存在则跳过）
    try:
        await ensure_collection()
    except Exception as e:
        logger.warning("Qdrant 集合初始化失败（如向量库未启动会在此提示）：%s", e)
    logger.info("服务启动完成，接口前缀：%s", settings.api_prefix)
    yield
    await engine.dispose()
    await close_client()
    logger.info("服务已关闭")


app = FastAPI(
    title=settings.app_name,
    description="企业级电商知识库 RAG 问答系统",
    version="0.1.0",
    lifespan=lifespan,
)

# 跨域：只允许配置里的前端开发地址访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载业务接口
app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/")
async def root():
    """根路径：给人看一眼服务基本信息。"""
    return {"app": settings.app_name, "docs": "/docs", "api_prefix": settings.api_prefix}
