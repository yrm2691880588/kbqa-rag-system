"""数据库连接与会话管理（异步 SQLAlchemy）。"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings


def _build_engine():
    """根据配置创建异步引擎；SQLite 需要额外的线程参数。"""
    kwargs = {"pool_pre_ping": True}  # 断线自动重连
    if settings.is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_async_engine(settings.database_url, echo=False, **kwargs)


engine = _build_engine()

# 会话工厂：每个请求从这里取一个数据库会话
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：为每个请求提供独立数据库会话，用完自动关闭。"""
    async with AsyncSessionLocal() as session:
        yield session
