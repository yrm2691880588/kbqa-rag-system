"""知识库表：类似"文件夹"，把商品资料分类管理，管理员维护。"""

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    # 名称唯一性从"全站唯一"改为"同组织内唯一"（见下方 org_id 说明）
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_knowledge_bases_org_name"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 归属组织（租户）。检索与列表一律按本列过滤，这是数据隔离的关键列。
    # 留成可空的原因同 users.org_id：给已有数据库补列时无法直接设 NOT NULL，
    # 一致性由应用层保证（见 core/tenancy.py 与 init_db 的回填逻辑）。
    org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # 名称：只在"同一组织内"唯一。不同客户各有一个叫"产品手册"的库是完全正常的。
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 冗余计数：文档数/片段数，展示用（以统计查询为准，逐步更新）
    doc_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
