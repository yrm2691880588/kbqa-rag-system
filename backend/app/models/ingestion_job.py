"""文档解析任务表（P2-15）：把"解析入库"变成一条条可重试、可恢复的任务。

为什么要从"进程内后台任务"换成"任务表"（大白话）：

    原来上传文档后，是直接在**当前进程内存里**起一个后台任务去解析。
    自己用没问题，但一到生产就有三个躲不掉的毛病：

    ① **进程一重启，正在解析的文档就永远卡在"解析中"了**。
       文件还在，但没人再去处理它，界面上那条进度条永远不会动，只能删了重传。

    ② **多进程部署时，任务只挂在某一个进程上**。
       那个进程恰好被打断（发版、OOM 被操作系统杀掉），任务就丢了。

    ③ **失败没有重试**。解析失败最常见的原因不是文件有问题，而是
       "调用向量服务时网络抖了一下"。没有重试就得人工重传。

    换成"把任务写进数据库，由工作进程去领"之后：
      · 进程重启 → 任务还在表里，重启后自动接着做（启动时把"僵尸任务"放回队列）；
      · 多进程 → 用数据库锁保证**同一个任务只会被一个进程领走**，不会重复解析；
      · 失败 → 自动重试几次，且每次等待时间翻倍（避免外部服务正抽风时反复撞上去）。

    另外还会**挡住重复入队**：同一个文档重复点上传，不该排三个任务把同一份文件解析三遍
    （那就白花三倍的钱，而且并发写同一个知识库还可能互相干扰）。
"""

import datetime as dt

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# 任务状态。字面量直接用字符串，便于运维直接看数据库（不用去查常量表）
PENDING = "pending"    # 排队中（或失败后等待重试）
RUNNING = "running"    # 正在被某个工作进程处理
DONE = "done"          # 成功
FAILED = "failed"      # 重试次数用完，彻底失败（需要人工介入）


class IngestionJob(Base):
    """一条文档解析任务。"""

    __tablename__ = "ingestion_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 要处理的文档与它所属的知识库
    doc_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )

    status: Mapped[str] = mapped_column(String(16), default=PENDING, index=True, nullable=False)

    # 已尝试次数 / 最多尝试几次。
    # 为什么要上限：遇到"这个 PDF 就是坏的"这种必然失败的情况，
    # 无限重试会变成一个永远在转的任务，白烧钱。
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    # 下次可以尝试的时间。失败后会被推后（指数退避），成功前则会立即被领取。
    # 有索引：工作进程每次都要按"到点了没"来捞任务，没索引会越来越慢。
    next_attempt_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 谁在什么时候领走了它。这两个字段是"重启恢复"的依据：
    # 如果一个任务一直是 running 但 locked_at 已经很久，说明领它的进程已经死了。
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
