"""add message feedback reason and timestamp

给消息表补两个反馈相关字段：

  feedback_reason  点踩时选的原因分类（如"回答不准确""没有引用到资料"）
  feedback_at      反馈时间

为什么需要这两个（而不是只用已有的 feedback_note）：

  1) feedback_reason —— 自由文本没法统计"哪类问题最常出现"。
     有了分类，后台才能回答"最近一周大家主要抱怨什么"，
     进而知道该补资料、该调检索、还是该换模型。

  2) feedback_at —— 与消息创建时间分开记，才能算出
     "用户是在看到回答后多久才评价的"。
     刚生成就点踩 和 隔了一周才点踩，含义完全不同。

Revision ID: 29c50d4ee624
Revises: d9384bc696d2
Create Date: 2026-11-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "29c50d4ee624"
down_revision: Union[str, None] = "d9384bc696d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 两个字段都允许为空：历史消息没有反馈，不需要回填。
    # 用 batch_alter_table 是为了兼容 SQLite（它不支持部分 ALTER 语法）。
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("feedback_reason", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("feedback_at", sa.DateTime(timezone=True), nullable=True)
        )
        # 后台按"有反馈的消息"筛选是高频操作，给 feedback 建个索引
        batch_op.create_index(batch_op.f("ix_messages_feedback"), ["feedback"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_messages_feedback"))
        batch_op.drop_column("feedback_at")
        batch_op.drop_column("feedback_reason")
