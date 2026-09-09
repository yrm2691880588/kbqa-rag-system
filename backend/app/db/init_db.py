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
        # 旧库升级：给已存在的 users 表补 phone 列（幂等，重复启动不报错）
        await conn.run_sync(_ensure_columns)

    await _seed_admin()
    logger.info("数据库初始化完成（建表 + 列迁移 + 管理员检查）")


def _ensure_columns(sync_conn) -> None:
    """轻量迁移：为已有 users 表补上新增的 phone 列（仅当列不存在时执行）。

    说明：Base.metadata.create_all 只会创建"缺失的表"，不会给已有表加列。
    这里用 IF NOT EXISTS 保证幂等 —— 每次启动都执行，但只有第一次真正生效。
    """
    from sqlalchemy import inspect, text

    insp = inspect(sync_conn)
    existing_cols = {c["name"] for c in insp.get_columns("users")}
    if "phone" not in existing_cols:
        sync_conn.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR(20)"))
        logger.info("已为 users 表补充 phone 列（手机号登录支持）")
    if "register_ip" not in existing_cols:
        sync_conn.execute(text("ALTER TABLE users ADD COLUMN register_ip VARCHAR(64)"))
        logger.info("已为 users 表补充 register_ip 列（防批量注册）")
    # 手机号唯一索引（NULL 可重复，所以不影响未绑手机号的老用户）
    sync_conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_phone ON users (phone)"))
    sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_register_ip ON users (register_ip)"))


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
