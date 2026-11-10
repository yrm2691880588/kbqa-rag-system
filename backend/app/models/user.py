"""用户表：登录账号、所属组织、角色（超管/组织管理员/成员）、密码哈希。"""

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 登录名：唯一、只允许字母数字下划线（注册时校验）
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    # 手机号：唯一（可空）。绑定后即可用"手机号+短信验证码"登录，与用户名登录是同一个账号
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True, nullable=True)
    # argon2 密码哈希，绝不存明文
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    nickname: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # 所属组织（租户）。这一列决定"这个人能看到哪些知识库"。
    # 为什么留成可空：①平台超管不隶属任何组织；②给已有数据库补列时无法直接设 NOT NULL。
    # 真正的一致性由应用层保证（见 core/tenancy.py 与 init_db 的回填逻辑）。
    org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 平台超管：能创建/管理所有组织（相当于 SaaS 运营方账号），不受组织隔离限制
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 组织内角色：admin=组织管理员(可管本组织知识库与成员) / user=普通成员(只能问答)
    role: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    # 注册来源 IP（用于限制同一 IP 每小时注册数，防脚本批量刷号）
    register_ip: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
