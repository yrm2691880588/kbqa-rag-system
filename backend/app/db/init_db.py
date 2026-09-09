"""启动初始化：建表 + 首次自动创建管理员 pjs。"""

import logging

from sqlalchemy import select

# 导入所有模型，让它们注册到 Base.metadata（create_all 才会建这些表）
from app import models  # noqa: F401
from app.core.config import settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import AsyncSessionLocal, engine

logger = logging.getLogger("kbqa.init")


async def init_db() -> None:
    """应用启动时调用：确保表存在，并保证管理员账号存在。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _seed_admin()
    logger.info("数据库初始化完成（建表 + 管理员检查）")


async def _seed_admin() -> None:
    """若管理员账号不存在则创建（用户名/密码取 .env 配置，默认 pjs/123456）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(models.User).where(models.User.username == settings.admin_username)
        )
        if result.scalar_one_or_none() is not None:
            return
        admin = models.User(
            username=settings.admin_username,
            password_hash=hash_password(settings.admin_password),
            nickname="系统管理员",
            role="admin",
        )
        session.add(admin)
        await session.commit()
        logger.info("已创建初始管理员账号：%s", settings.admin_username)
