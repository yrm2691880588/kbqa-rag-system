"""用户表：登录账号、角色（admin/user）、密码哈希。"""

import datetime as dt

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 登录名：唯一、只允许字母数字下划线（注册时校验）
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    # argon2 密码哈希，绝不存明文
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    nickname: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # 角色：admin=管理员(可管知识库) / user=普通用户(只能问答)
    role: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
