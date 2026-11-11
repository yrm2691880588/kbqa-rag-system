"""仪表盘接口：一进来就能看到"系统整体怎么样"。

这个接口要回答的问题（也是管理员打开系统第一个想知道的）：
    1) 今天有多少人在用？问了多少次？
    2) 答上率如何？（有多少问题系统没能从知识库答上来）
    3) 用户满意吗？（赞/踩的比例、有多少条待处理的不满意）
    4) 资料入库正常吗？（有多少文档还在解析、多少失败）
    5) 额度快用完了吗？

设计取舍：
    · 不新建表 —— 全部从现有表聚合，避免"为了个看板再造一套数据"。
    · 一次性返回 —— 前端只发一个请求，避免首屏 5 个接口并发。
    · 按组织收敛 —— 与其它接口同一套 _scope_org 口径。
"""

import datetime as dt
import logging

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select

from app.core.deps import AdminUser, DbSession, is_superadmin
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.message import Message
from app.models.organization import Organization
from app.models.query_log import QueryLog
from app.models.session import ChatSession
from app.models.user import User
from app.schemas.dashboard import DashboardOut

logger = logging.getLogger("kbqa.api.dashboard")

router = APIRouter(prefix="/dashboard", tags=["仪表盘"])


def _scope_org(current_user: User) -> int | None:
    """能看的组织范围。None 只代表"平台超管，不限"。

    与 query_logs / feedbacks 保持同一套口径。
    （那边踩过坑：org_id 为 None 的普通账号如果也返回 None，
     等于把"不限"权限发给了没归属的账号。）
    """
    if is_superadmin(current_user):
        return None
    if current_user.org_id is None:
        return -1
    return current_user.org_id


@router.get("", response_model=DashboardOut)
async def get_dashboard(
    current_user: AdminUser,
    db: DbSession,
    days: int = Query(7, ge=1, le=90, description="趋势统计最近多少天"),
):
    """仪表盘总览数据。

    为什么趋势只给"每天提问数"一项，而不是把满意率、反馈数也做成趋势：
        数据量小时，按天的满意率会是 0/1 这种毫无意义的数字。
        先把最有信息量的一个趋势做实，比堆一堆空图表好。
    """
    org_id = _scope_org(current_user)
    today = dt.date.today()
    since = dt.datetime.combine(today - dt.timedelta(days=days - 1), dt.time.min)
    today_start = dt.datetime.combine(today, dt.time.min)

    # ---------- 1) 用户与知识库规模 ----------
    user_stmt = select(func.count(User.id)).where(User.is_active.is_(True))
    kb_stmt = select(func.count(KnowledgeBase.id)).where(KnowledgeBase.is_active.is_(True))
    if org_id is not None:
        user_stmt = user_stmt.where(User.org_id == org_id)
        kb_stmt = kb_stmt.where(KnowledgeBase.org_id == org_id)
    user_count = int(await db.scalar(user_stmt) or 0)
    kb_count = int(await db.scalar(kb_stmt) or 0)

    # ---------- 2) 提问量（今天 / 最近 N 天）----------
    # 提问表里没有 org_id，要沿 user 收敛
    def _scoped_query_log():
        q = select(func.count(QueryLog.id)).select_from(QueryLog).join(
            User, User.id == QueryLog.user_id
        )
        if org_id is not None:
            q = q.where(User.org_id == org_id)
        return q

    today_questions = int(
        await db.scalar(
            _scoped_query_log().where(QueryLog.created_at >= today_start)
        )
        or 0
    )
    period_questions = int(
        await db.scalar(_scoped_query_log().where(QueryLog.created_at >= since)) or 0
    )

    # ---------- 3) 答上率（最近 N 天）----------
    answered_stmt = (
        select(
            func.count(QueryLog.id),
            func.sum(case((QueryLog.answered.is_(True), 1), else_=0)),
        )
        .select_from(QueryLog)
        .join(User, User.id == QueryLog.user_id)
        .where(QueryLog.created_at >= since)
    )
    if org_id is not None:
        answered_stmt = answered_stmt.where(User.org_id == org_id)
    row = (await db.execute(answered_stmt)).one()
    total_q = int(row[0] or 0)
    answered_q = int(row[1] or 0)
    answer_rate = round(answered_q / total_q * 100, 1) if total_q else None

    # ---------- 4) 满意度（全部时间，因为反馈量本来就少）----------
    fb_stmt = (
        select(
            func.count(Message.id),
            func.sum(case((Message.feedback == 1, 1), else_=0)),
            func.sum(case((Message.feedback == -1, 1), else_=0)),
        )
        .join(ChatSession, ChatSession.id == Message.session_id)
        .join(User, User.id == ChatSession.user_id)
        .where(Message.feedback.isnot(None))
    )
    if org_id is not None:
        fb_stmt = fb_stmt.where(User.org_id == org_id)
    fb_row = (await db.execute(fb_stmt)).one()
    fb_total = int(fb_row[0] or 0)
    fb_likes = int(fb_row[1] or 0)
    fb_dislikes = int(fb_row[2] or 0)
    satisfaction = round(fb_likes / fb_total * 100, 1) if fb_total else None

    # ---------- 5) 文档入库状态 ----------
    doc_stmt = (
        select(Document.status, func.count(Document.id))
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .group_by(Document.status)
    )
    if org_id is not None:
        doc_stmt = doc_stmt.where(KnowledgeBase.org_id == org_id)
    doc_rows = (await db.execute(doc_stmt)).all()
    doc_by_status = {status: int(n) for status, n in doc_rows}
    processing = sum(
        doc_by_status.get(s, 0) for s in ("pending", "parsing", "embedding")
    )

    # ---------- 6) 每天提问趋势 ----------
    trend_stmt = (
        select(
            func.date(QueryLog.created_at).label("d"),
            func.count(QueryLog.id),
        )
        .select_from(QueryLog)
        .join(User, User.id == QueryLog.user_id)
        .where(QueryLog.created_at >= since)
        .group_by(func.date(QueryLog.created_at))
        .order_by(func.date(QueryLog.created_at))
    )
    if org_id is not None:
        trend_stmt = trend_stmt.where(User.org_id == org_id)
    trend_rows = (await db.execute(trend_stmt)).all()
    trend_map = {str(d): int(n) for d, n in trend_rows}

    # 补齐没有数据的日期（图表的横轴要连续，否则会跳日看起来很奇怪）
    trend = []
    for i in range(days):
        d = today - dt.timedelta(days=days - 1 - i)
        key = d.isoformat()
        trend.append({"date": key, "count": trend_map.get(key, 0)})

    # ---------- 7) 最近的活动（最近 5 条提问）----------
    recent_stmt = (
        select(QueryLog.question, QueryLog.answered, QueryLog.created_at, User.username)
        .join(User, User.id == QueryLog.user_id)
        .order_by(QueryLog.created_at.desc())
        .limit(5)
    )
    if org_id is not None:
        recent_stmt = recent_stmt.where(User.org_id == org_id)
    recent_rows = (await db.execute(recent_stmt)).all()
    recent = [
        {
            "question": q,
            "answered": bool(a),
            "created_at": c,
            "username": u,
        }
        for q, a, c, u in recent_rows
    ]

    return DashboardOut(
        user_count=user_count,
        kb_count=kb_count,
        doc_count=sum(doc_by_status.values()),
        processing_docs=processing,
        failed_docs=doc_by_status.get("failed", 0),
        today_questions=today_questions,
        period_questions=period_questions,
        answer_rate=answer_rate,
        feedback_total=fb_total,
        feedback_likes=fb_likes,
        feedback_dislikes=fb_dislikes,
        satisfaction_rate=satisfaction,
        trend=trend,
        recent=recent,
    )
