"""每日用量表：按「组织 + 日期」累计当天消耗了多少。

为什么按天存一行、而不是每次调用存一行明细：
    ① 查额度只需要"今天用了多少""这个月用了多少"，按天聚合查一次就够，
       不用扫几十万条明细；
    ② 明细其实已经有了 —— query_logs 里每次提问都记着（谁、问了什么、耗时），
       真要审计就去看那张表。这张表只负责"快算"。

唯一约束 (org_id, day) 很关键：它保证同一个组织同一天只会有一行，
不会因为并发两条请求各插一行导致用量被拆成两份、额度算不准。
"""

import datetime as dt

from sqlalchemy import Date, DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UsageDaily(Base):
    __tablename__ = "usage_daily"
    __table_args__ = (
        UniqueConstraint("org_id", "day", name="uq_usage_daily_org_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    day: Mapped[dt.date] = mapped_column(Date, index=True, nullable=False)

    # 提问次数（一问一答算 1 次）
    questions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 大模型用量：输入/输出 token（上游没返回时保持 0）
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 文档入库时的向量化用量（按文本块数计，比 token 更贴近实际计费口径）
    embedding_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
