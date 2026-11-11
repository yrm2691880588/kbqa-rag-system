"""反馈管理接口：把用户点的"踩"变成可行动的改进清单。

背景：
    消息表一直有 feedback 字段（赞/踩），但没有读取入口 ——
    用户点了踩也不会有人看到。这个接口补上"看回去"的能力。

它要回答三个问题：
    1) 整体满意度如何？（赞多少、踩多少、踩的比例）
    2) 大家主要在抱怨什么？（按原因分类统计）
    3) 具体哪几条答得不好？（列表 + 详情，能点开看原问题和回答）

权限与范围（关键）：
    消息表里没有 org_id，只有 session_id → user_id。
    所以要"顺藤摸瓜"：消息 → 会话 → 用户 → 组织。
    绝不能直接返回全部消息，否则 A 公司的管理员能看到 B 公司用户的对话内容。
"""

import logging

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import AdminUser, DbSession, is_superadmin
from app.models.message import Message
from app.models.session import ChatSession
from app.models.user import User
from app.schemas.feedback import (
    FEEDBACK_REASON_LABELS_CN,
    FeedbackItemOut,
    FeedbackStatsOut,
)

logger = logging.getLogger("kbqa.api.feedbacks")

router = APIRouter(prefix="/feedbacks", tags=["用户反馈"])


def _scope_org(current_user: User) -> int | None:
    """操作者能看的组织范围。None 只代表"平台超管，不限"。

    与 query_logs 保持同一套口径（那边踩过坑：org_id 为 None 的普通账号
    如果也返回 None，就等于把"不限"权限发给了没归属的账号）。
    """
    if is_superadmin(current_user):
        return None
    if current_user.org_id is None:
        return -1
    return current_user.org_id


def _base_query(org_id: int | None, feedback: int | None):
    """构造"已按组织收敛"的基础查询。

    为什么每次都要 join 两层：
        消息表没有 org_id，只能沿 message → session → user 找到组织。
        漏掉任何一层 join，范围收敛就会失效（A 公司看到 B 公司数据）。
    """
    stmt = (
        select(Message, ChatSession, User)
        .join(ChatSession, ChatSession.id == Message.session_id)
        .join(User, User.id == ChatSession.user_id)
        .where(Message.feedback.isnot(None))     # 只看有评价的消息
    )
    if org_id is not None:
        stmt = stmt.where(User.org_id == org_id)
    if feedback is not None:
        stmt = stmt.where(Message.feedback == feedback)
    return stmt


@router.get("", response_model=list[FeedbackItemOut])
async def list_feedbacks(
    current_user: AdminUser,
    db: DbSession,
    feedback: int | None = Query(None, description="1=只看赞 / -1=只看踩；不传则全部"),
    reason: str | None = Query(None, description="按原因分类筛选，如 inaccurate"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """反馈列表：管理员看"哪些回答被用户评价了、评价成什么"。

    默认按反馈时间倒序 —— 最近被吐槽的排最前面，方便优先处理。
    """
    org_id = _scope_org(current_user)
    stmt = _base_query(org_id, feedback)

    if reason:
        stmt = stmt.where(Message.feedback_reason == reason)

    stmt = stmt.order_by(Message.feedback_at.desc().nullslast()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).all()

    return [
        FeedbackItemOut(
            message_id=msg.id,
            session_id=msg.session_id,
            feedback=msg.feedback,
            reason=msg.feedback_reason,
            reason_label=FEEDBACK_REASON_LABELS_CN.get(msg.feedback_reason or "", None),
            note=msg.feedback_note,
            feedback_at=msg.feedback_at,
            question=_previous_question_text(sess, msg),
            answer=msg.content,
            username=user.username,
            citations=msg.citations,
            created_at=msg.created_at,
        )
        for msg, sess, user in rows
    ]


def _previous_question_text(session: ChatSession, msg: Message) -> str:
    """取这条回答对应的问题原文。

    为什么用会话标题而不是再查一次消息表：
        会话标题就是用户第一句提问（创建会话时自动取的），
        对"这条回答答得好不好"的判断已经够用，省一次往返查询。
        真正需要精确问题全文的场景在详情页（会单独查历史消息）。
    """
    return session.title or ""


@router.get("/stats", response_model=FeedbackStatsOut)
async def feedback_stats(current_user: AdminUser, db: DbSession):
    """反馈总览：满意度、各原因分布。

    为什么单独的统计接口而不是让前端算：
        前端只能看到当前这一页（默认 50 条），算出来的比例是错的。
        聚合必须由数据库做才能反映全量。
    """
    org_id = _scope_org(current_user)

    # 用一条聚合查询同时算总数/赞/踩：
    # 为什么用 case(...) 求和而不是查三次：
    #   三次独立查询之间数据可能变化，算出来的赞+踩可能不等于总数；
    #   一条语句在同一个快照里算完，数字自洽。
    from sqlalchemy import case

    agg_stmt = (
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
        agg_stmt = agg_stmt.where(User.org_id == org_id)

    row = (await db.execute(agg_stmt)).one()
    total_i = int(row[0] or 0)
    like_i = int(row[1] or 0)
    dislike_i = int(row[2] or 0)

    # 按原因分组统计（只看踩的；赞没有原因）
    reason_stmt = (
        select(Message.feedback_reason, func.count(Message.id))
        .join(ChatSession, ChatSession.id == Message.session_id)
        .join(User, User.id == ChatSession.user_id)
        .where(Message.feedback == -1, Message.feedback_reason.isnot(None))
        .group_by(Message.feedback_reason)
    )
    if org_id is not None:
        reason_stmt = reason_stmt.where(User.org_id == org_id)

    reason_rows = (await db.execute(reason_stmt)).all()

    reason_counts = []
    for reason_key, n in reason_rows:
        if not reason_key:
            continue
        reason_counts.append(
            {
                "reason": reason_key,
                "label": FEEDBACK_REASON_LABELS_CN.get(reason_key, reason_key),
                "count": int(n),
            }
        )
    reason_counts.sort(key=lambda x: -x["count"])

    # 满意率 = 赞 / 总评价数。没有评价时返回 None（而不是 0，
    # 避免"0% 满意度"这种误导性数字）。
    rate = round(like_i / total_i * 100, 1) if total_i else None

    return FeedbackStatsOut(
        total=total_i,
        likes=like_i,
        dislikes=dislike_i,
        satisfaction_rate=rate,
        reason_counts=reason_counts,
    )


@router.get("/{message_id}", response_model=FeedbackItemOut)
async def feedback_detail(message_id: int, current_user: AdminUser, db: DbSession):
    """单条反馈详情（含完整问答），用于排查"到底哪里答错了"。"""
    org_id = _scope_org(current_user)
    row = (
        await db.execute(_base_query(org_id, None).where(Message.id == message_id))
    ).first()

    if row is None:
        # 越权与不存在一律 404：403 等于告诉对方"这条存在但你没权限"
        raise HTTPException(status.HTTP_404_NOT_FOUND, "反馈不存在")

    msg, sess, user = row
    return FeedbackItemOut(
        message_id=msg.id,
        session_id=msg.session_id,
        feedback=msg.feedback,
        reason=msg.feedback_reason,
        reason_label=FEEDBACK_REASON_LABELS_CN.get(msg.feedback_reason or "", None),
        note=msg.feedback_note,
        feedback_at=msg.feedback_at,
        question=_previous_question_text(sess, msg),
        answer=msg.content,
        username=user.username,
        citations=msg.citations,
        created_at=msg.created_at,
    )
