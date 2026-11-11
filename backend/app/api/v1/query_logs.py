"""问答日志接口：把"只写不读"的日志变成运营能看的数据。

背景：系统一直有在记 query_logs（谁、问了什么、有没有答上、检索命中什么、耗时多久），
但**没有任何读取入口**——等于白记。管理员看到的只有"用户说答得不准"，
却拿不到"到底哪些问题答不上来"的清单。

权限与范围（关键）：
    日志表里**没有 org_id**，只有 user_id。所以要"顺藤摸瓜"：
    日志 → 用户 → 组织。绝不能直接按 user_id 分组返回，
    否则 A 公司的管理员就能看到 B 公司用户问了什么问题。
"""

import logging

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import case, func, select

from app.core.deps import AdminUser, DbSession
from app.models.query_log import QueryLog
from app.models.user import User
from app.schemas.query_log import QueryLogOut, QueryLogStats, QueryLogDetailOut

logger = logging.getLogger("kbqa.api.query_logs")

router = APIRouter(prefix="/query-logs", tags=["问答日志"])


def _scope_org(current_user: User) -> int | None:
    """操作者能看的组织范围。None 代表"平台超管，不限"。

    ⚠️ 注意 None 有且只有一个含义：**平台超管**。
    普通账号如果 org_id 为 None（例如组织被删后外键 SET NULL），
    绝不能让它也返回 None —— 那等于把"不限"的权限发给了一个没归属的账号，
    会看到全平台所有组织的问答日志（含客户真实问过的业务问题）。
    这种情况改用 -1 作为"必然匹配不到任何数据"的哨兵值。
    """
    if getattr(current_user, "is_superadmin", False):
        return None
    if current_user.org_id is None:
        return -1
    return current_user.org_id


@router.get("", response_model=list[QueryLogOut])
async def list_query_logs(
    current_user: AdminUser,
    db: DbSession,
    answered: bool | None = Query(None, description="只看答上的 / 只看没答上的"),
    keyword: str | None = Query(None, max_length=100, description="按问题内容模糊搜"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """问答日志列表（按时间倒序）。"""
    org_id = _scope_org(current_user)
    if org_id is None and not getattr(current_user, "is_superadmin", False):
        return []

    # 顺藤摸瓜：日志 → 用户 → 组织。用 join 而不是先查用户 id 列表，
    # 是为了数据库一次算完，用户多了也不会把 id 列表撑爆。
    stmt = (
        select(QueryLog, User.username)
        .join(User, User.id == QueryLog.user_id)
        .order_by(QueryLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if org_id is not None:
        stmt = stmt.where(User.org_id == org_id)
    if answered is not None:
        stmt = stmt.where(QueryLog.answered.is_(answered))
    if keyword:
        stmt = stmt.where(QueryLog.question.ilike(f"%{keyword.strip()}%"))

    rows = (await db.execute(stmt)).all()
    out: list[QueryLogOut] = []
    for log, username in rows:
        out.append(
            QueryLogOut(
                id=log.id,
                user_id=log.user_id,
                username=username,
                session_id=log.session_id,
                question=log.question,
                kb_scope=log.kb_scope,
                answered=log.answered,
                latency_ms=log.latency_ms,
                created_at=log.created_at,
                hit_count=len(log.retrieval or []),
            )
        )
    return out


@router.get("/stats", response_model=QueryLogStats)
async def query_log_stats(current_user: AdminUser, db: DbSession):
    """概览：总提问数、未答上数、平均耗时。

    管理员打开这个页面第一个想知道的不是"具体某一条"，而是
    "整体答得怎么样"。所以单独给一个汇总，省得前端自己算（也算不准）。
    """
    org_id = _scope_org(current_user)
    # 未答上的条数用 CASE WHEN 求和来数：比"查一次总数、再查一次未答上"少打一次库，
    # 也避免两次查询之间数据变化导致两个数对不上。
    unanswered_expr = func.sum(case((QueryLog.answered.is_(False), 1), else_=0))
    base = (
        select(
            func.count(QueryLog.id),
            unanswered_expr,
            func.coalesce(func.avg(QueryLog.latency_ms), 0),
        )
        .select_from(QueryLog)
        .join(User, User.id == QueryLog.user_id)
    )
    if org_id is not None:
        base = base.where(User.org_id == org_id)
    row = (await db.execute(base)).one()

    total = int(row[0] or 0)
    unanswered = int(row[1] or 0)
    return QueryLogStats(
        total=total,
        unanswered=unanswered,
        answered=total - unanswered,
        avg_latency_ms=int(row[2] or 0),
    )


@router.get("/{log_id}", response_model=QueryLogDetailOut)
async def get_query_log(log_id: int, current_user: AdminUser, db: DbSession):
    """单条日志详情（含检索命中明细，用于诊断"为什么这题没答好"）。"""
    log = await db.get(QueryLog, log_id)
    if log is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "日志不存在")

    org_id = _scope_org(current_user)
    if org_id is not None:
        # 顺藤摸瓜校验归属：这条日志必须属于本组织的某个用户
        owner_org = None
        if log.user_id is not None:
            owner = await db.get(User, log.user_id)
            owner_org = owner.org_id if owner else None
        if owner_org != org_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "日志不存在")

    username = None
    if log.user_id is not None:
        owner = await db.get(User, log.user_id)
        username = owner.username if owner else None

    return QueryLogDetailOut(
        id=log.id,
        user_id=log.user_id,
        username=username,
        session_id=log.session_id,
        question=log.question,
        kb_scope=log.kb_scope,
        retrieval=log.retrieval,
        answered=log.answered,
        latency_ms=log.latency_ms,
        created_at=log.created_at,
    )
