"""文档表：知识库里的每份上传文件，记录入库状态（支持断点续传）。"""

import datetime as dt

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # 文件内容哈希：同一文件重复上传时自动识别（去重）
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    # 文档类型：docx / pdf / xlsx / txt / md / html
    doc_type: Mapped[str] = mapped_column(String(10), nullable=False)
    # 入库状态机：pending(排队) → parsing(解析中) → embedding(向量化中) → ready(完成) / failed(失败)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    # 入库进度百分比 0-100（embedding 分批进行时实时更新，前端进度条用）
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # 重传覆盖自动+1
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
