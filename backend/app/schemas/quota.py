"""额度相关的接口数据结构（P1-B）。"""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class QuotaOut(BaseModel):
    """某组织的额度配置。"""

    model_config = ConfigDict(from_attributes=True)

    org_id: int
    daily_question_limit: int | None = None
    monthly_question_limit: int | None = None
    daily_token_limit: int | None = None
    monthly_token_limit: int | None = None
    is_service_enabled: bool = True
    note: str = ""
    updated_at: dt.datetime | None = None


class QuotaUpdate(BaseModel):
    """修改额度。

    这里**刻意允许"不传某个字段"和"传 null"是两种意思**：
        · 请求体里根本没写 daily_question_limit → 这一项保持原样不动；
        · 写了 "daily_question_limit": null     → 这一项改成"不限"。
    实现方式是接口里用 exclude_unset 取"用户真正传了的字段"。
    为什么较真：如果没有这个区分，运营只想改一下备注，
    却会把所有额度顺手清空成"不限"——一次静默的、非常大的失误。
    """

    daily_question_limit: int | None = Field(default=None, ge=0, le=10_000_000)
    monthly_question_limit: int | None = Field(default=None, ge=0, le=100_000_000)
    daily_token_limit: int | None = Field(default=None, ge=0, le=2_000_000_000)
    monthly_token_limit: int | None = Field(default=None, ge=0, le=20_000_000_000)
    is_service_enabled: bool | None = None
    note: str | None = Field(default=None, max_length=255)


class UsageBucket(BaseModel):
    """一个时间口径（今天 / 本月）的用量与上限。"""

    questions: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    embedding_chunks: int = 0
    question_limit: int | None = None
    token_limit: int | None = None


class DailyBucket(UsageBucket):
    """今日用量（多一个日期字段）。"""

    day: dt.date


class UsageSummaryOut(BaseModel):
    """用量的对外汇总。"""

    org_id: int
    is_service_enabled: bool = True
    daily: DailyBucket
    monthly: UsageBucket
