"""消息表：会话中的一问一答，含引用快照与用户反馈。"""

import datetime as dt

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    # user=用户提问 / assistant=AI 回答
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 引用片段快照（JSON）：回答生成那一刻拷贝原文，源文档日后删除也不影响查看
    citations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 用户反馈：1=赞 / -1=踩 / NULL=未反馈
    # 建索引：后台"只看有评价的消息"是高频查询，无索引会全表扫描
    feedback: Mapped[int | None] = mapped_column(SmallInteger, nullable=True, index=True)
    # 点踩时选择的原因分类（见 schemas/chat.py 的 FEEDBACK_REASONS）。
    # 为什么单独存分类而不是只存自由文本：
    #   自由文本没法统计"哪类问题最常出现"，而分类可以。
    #   两者并存：分类用于聚合看趋势，说明用于看细节。
    feedback_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    feedback_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 反馈时间：与消息创建时间分开，才能统计"多久之后才被评价"
    feedback_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 单次回答耗时
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
