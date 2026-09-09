"""问答日志表：记录每次提问与命中情况，供管理员观察资料覆盖质量。"""

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QueryLog(Base):
    __tablename__ = "query_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    kb_scope: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 检索命中的 top 片段与分数（JSON），便于诊断为什么没答好
    retrieval: Mapped[list | None] = mapped_column(JSON, nullable=True)
    answered: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # 是否成功产出回答
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
