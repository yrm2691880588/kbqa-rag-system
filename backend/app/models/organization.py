"""组织（租户）表：一家企业 = 一个组织。

为什么需要它（大白话）：
    之前系统里所有知识库是"全站共用"的——你在管理页建一个库，任何登录用户都能看到、
    都能检索。这在一个公司内部用没问题，但**一旦要卖给多家企业就不行了**：
    A 公司的资料绝不能被 B 公司的人搜到。

    所以引入「组织」这一层：用户属于某个组织，知识库也属于某个组织，
    所有查询都强制只在本组织范围内进行。这就是"多租户隔离"。

角色关系（三段）：
    平台超管（User.is_superadmin=True） → 建组织、管所有组织（相当于你自己，SaaS 运营方）
    组织管理员（User.role="admin"）      → 管本组织的知识库与成员
    组织成员（User.role="user"）         → 只能问答
"""

import datetime as dt

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Organization(Base):
    """组织（客户/租户）。"""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 组织名称：全局唯一（同名组织会让邀请码与展示产生歧义）
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 邀请码：新用户注册时凭码加入该组织。
    # 为什么不用"直接填组织名"：组织名是可以被猜到的（比如试"某某公司"），
    # 而邀请码是随机串，猜不中，等于一道准入凭证。
    invite_code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
