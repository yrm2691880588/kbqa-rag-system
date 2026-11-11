"""反馈管理接口的数据结构。

与 schemas/chat.py 的 FEEDBACK_REASONS 是同一份分类：
那边是"用户提交时校验"，这边是"后台展示时翻译成中文"。
两边共用同一份 key 定义，避免改了分类却忘了改展示文案。
"""

import datetime as dt

from pydantic import BaseModel, ConfigDict

from app.schemas.chat import FEEDBACK_REASONS

# 反查表：key -> 中文标签。从 FEEDBACK_REASONS 直接推导，
# 不手写第二份 —— 否则将来加分类时容易漏改一处，出现"有 key 没标签"。
FEEDBACK_REASON_LABELS_CN: dict[str, str] = dict(FEEDBACK_REASONS)


class FeedbackItemOut(BaseModel):
    """一条用户反馈（含被评价的那轮问答）。"""

    model_config = ConfigDict(from_attributes=True)

    message_id: int
    session_id: int
    # 1=赞 / -1=踩
    feedback: int | None = None
    reason: str | None = None
    reason_label: str | None = None
    note: str | None = None
    feedback_at: dt.datetime | None = None

    question: str = ""
    answer: str = ""
    username: str = ""
    citations: list | None = None
    created_at: dt.datetime | None = None


class FeedbackStatsOut(BaseModel):
    """反馈总览。

    satisfaction_rate 为 None 表示"还没有任何评价" ——
    区别于 0（有评价但全是踩）。前端要能把这两种情况显示得不一样。
    """

    total: int = 0
    likes: int = 0
    dislikes: int = 0
    satisfaction_rate: float | None = None
    # [{reason, label, count}, ...] 按 count 倒序
    reason_counts: list[dict] = []
