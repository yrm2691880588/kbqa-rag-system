"""会话与问答相关的请求/响应数据格式（M3）。"""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SessionCreate(BaseModel):
    """新建会话：可选指定默认检索的知识库范围。"""

    kb_ids: list[int] | None = Field(default=None, description="限定问答的知识库 id 列表；空=全部库")


class SessionUpdate(BaseModel):
    """更新会话：改标题（自动标题不满意可手改）。"""

    title: str | None = Field(default=None, min_length=1, max_length=100)


class SessionOut(BaseModel):
    """会话列表项：带首条消息摘要，方便侧边栏展示。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    kb_scope: list[int] | None = None
    message_count: int = 0
    created_at: dt.datetime
    updated_at: dt.datetime


class MessageOut(BaseModel):
    """单条消息（历史加载 / 实时追加）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    role: str  # user / assistant
    content: str
    citations: list | None = None   # assistant 回答的引用快照
    feedback: int | None = None
    created_at: dt.datetime


class ChatRequest(BaseModel):
    """发起一轮问答：session_id + 问题。知识库范围跟随会话（会话创建/更新时设定）。"""

    session_id: int = Field(description="会话 id（必须属于当前用户）")
    question: str = Field(min_length=1, max_length=2000, description="用户问题")


class FeedbackRequest(BaseModel):
    """给某条 AI 回答打「赞 / 踩 / 取消」。

    取值约定（与数据库 messages.feedback 字段一一对应）：
        1  = 赞（回答有帮助）
        -1 = 踩（回答不准确）
        0  = 取消之前的评价（落库时存 NULL，而不是存 0）

    为什么要单独给"取消"留一个值：
        用户点错了想收回，如果只能覆盖不能取消，就会产生一条假的不满意记录，
        污染后续"哪些问题答得不好"的统计。
    """

    feedback: int = Field(description="1=赞 / -1=踩 / 0=取消评价")
    note: str | None = Field(default=None, max_length=500, description="可选补充说明（踩的时候填）")

    @field_validator("feedback")
    @classmethod
    def _check_range(cls, v: int) -> int:
        """只允许 1 / -1 / 0 三个值，防止前端传进来一个没意义的数字。"""
        if v not in (1, -1, 0):
            raise ValueError("反馈值只能是 1（赞）、-1（踩）或 0（取消）")
        return v
