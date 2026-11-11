"""仪表盘接口的数据结构。"""

import datetime as dt

from pydantic import BaseModel


class TrendPoint(BaseModel):
    """趋势图上的一个点。"""

    date: str
    count: int


class RecentActivity(BaseModel):
    """最近的一条提问活动。"""

    question: str
    answered: bool
    created_at: dt.datetime | None = None
    username: str = ""


class DashboardOut(BaseModel):
    """仪表盘总览。

    关于 None 与 0 的区别（前端要能区分这两个）：
      · answer_rate / satisfaction_rate 为 None = "还没有数据可算"
      · 为 0 = "有数据，但全都没答上 / 全都不满意"
      把"没数据"显示成 0% 会误导人以为系统很差。
    """

    # 规模
    user_count: int = 0
    kb_count: int = 0
    doc_count: int = 0
    processing_docs: int = 0
    failed_docs: int = 0

    # 使用量
    today_questions: int = 0
    period_questions: int = 0

    # 质量
    answer_rate: float | None = None
    feedback_total: int = 0
    feedback_likes: int = 0
    feedback_dislikes: int = 0
    satisfaction_rate: float | None = None

    # 图表与活动
    trend: list[TrendPoint] = []
    recent: list[RecentActivity] = []
