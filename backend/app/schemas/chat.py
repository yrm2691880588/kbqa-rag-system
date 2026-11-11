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
    feedback_reason: str | None = None
    feedback_note: str | None = None
    feedback_at: dt.datetime | None = None
    created_at: dt.datetime


class ChatRequest(BaseModel):
    """发起一轮问答：session_id + 问题。知识库范围跟随会话（会话创建/更新时设定）。"""

    session_id: int = Field(description="会话 id（必须属于当前用户）")
    question: str = Field(min_length=1, max_length=2000, description="用户问题")
    regenerate: bool = Field(
        default=False,
        description=(
            "true = 对同一问题重新生成回答。"
            "服务端会忽略 question 字段，取会话里最后一条用户提问重跑，"
            "且**不把上一轮回答放进历史**（否则模型只是复述旧答案）。"
        ),
    )


# ============================================================
# 反馈原因分类（点踩时选择）
# ============================================================
# 为什么要预定义而不是让用户自由填：
#   自由文本没法聚合统计。有了固定分类，后台才能回答
#   "最近大家主要在抱怨什么" —— 进而知道该补资料、该调检索、还是该换模型。
#   自由文本仍然保留（feedback_note），用于看具体细节。
#
# 每个分类都对应一种**可行动的改进方向**，而不是笼统的"不满意"：
FEEDBACK_REASONS: dict[str, str] = {
    # key = 存进数据库的值（英文，稳定不变）
    # value = 界面展示的中文
    "inaccurate": "回答不准确",
    "no_reference": "没有引用到相关资料",
    "irrelevant": "答非所问",
    "incomplete": "回答不完整",
    "wrong_citation": "引用错了文档",
    "outdated": "资料过时",
    "other": "其他",
}


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
    reason: str | None = Field(
        default=None,
        description="点踩时的原因分类，取值见 FEEDBACK_REASONS 的 key；赞时忽略",
    )
    note: str | None = Field(
        default=None, max_length=500, description="可选补充说明（踩的时候填，自由文本）"
    )

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, v: str | None) -> str | None:
        """原因必须是预定义的 key，否则拒绝。

        为什么要校验：如果放开任意字符串，前端改一个字就可能写进一个
        永远统计不到的脏值（比如 "inaccurate " 带空格）。校验后
        数据库里只会出现这 7 个 key，聚合查询才可靠。
        """
        if v is None or v == "":
            return None
        if v not in FEEDBACK_REASONS:
            raise ValueError(
                f"未知的反馈原因 {v!r}，可选值：{', '.join(FEEDBACK_REASONS)}"
            )
        return v

    @field_validator("feedback")
    @classmethod
    def _check_range(cls, v: int) -> int:
        """只允许 1 / -1 / 0 三个值，防止前端传进来一个没意义的数字。"""
        if v not in (1, -1, 0):
            raise ValueError("反馈值只能是 1（赞）、-1（踩）或 0（取消）")
        return v
