"""会话与问答相关的请求/响应数据格式（M3）。"""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


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
