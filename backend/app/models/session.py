"""对话会话表：每个用户可有多条独立会话，历史记录按会话找回。"""

import datetime as dt

from sqlalchemy import DateTime, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ChatSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # 会话标题：初始"新对话"，首轮问答后由 AI 自动生成简短标题
    title: Mapped[str] = mapped_column(String(100), default="新对话", nullable=False)
    # 检索范围：None=全部知识库；否则为知识库 id 列表（用户提问时可自选单一库）
    kb_scope: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
