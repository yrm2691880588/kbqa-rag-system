"""问答日志的接口数据结构。"""

import datetime as dt

from pydantic import BaseModel, ConfigDict


class QueryLogOut(BaseModel):
    """日志列表项。

    这里**故意不返回 retrieval（检索命中明细）**：
        那是每个问题命中的若干段原文片段，一条就可能几十 KB。
        列表一页 50 条，全带上会让响应变得很大、页面很卡。
        要看的用户点进详情（GET /query-logs/{id}）再取。
    只留一个 hit_count（命中几段），足够在列表上判断"这题检索到东西没有"。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None = None
    username: str | None = None      # 提问人（join 出来的，日志表里没存）
    session_id: int | None = None
    question: str
    kb_scope: list | None = None
    answered: bool = True
    latency_ms: int | None = None
    created_at: dt.datetime
    hit_count: int = 0


class QueryLogDetailOut(QueryLogOut):
    """日志详情：多带检索命中明细，用于诊断"为什么这题没答好"。"""

    retrieval: list | None = None


class QueryLogStats(BaseModel):
    """日志概览统计。"""

    total: int = 0
    answered: int = 0
    unanswered: int = 0
    avg_latency_ms: int = 0
