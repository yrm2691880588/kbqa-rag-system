"""会话管理接口（M3）：每个用户独立的会话列表、创建、改名、删除、历史消息拉取。

数据模型关系：user 1—N session 1—N message。
权限：所有操作仅限**当前登录用户自己的会话**（后端强校验 session.user_id == 当前用户），
管理员也不能看别人的会话。
"""

import datetime as dt
import logging

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import func, select, or_

from app.core.deps import CurrentUser, DbSession
from app.domain import get_domain
from app.core.tenancy import resolve_kb_scope
from app.models.message import Message
from app.models.session import ChatSession
from app.schemas.chat import FeedbackRequest, MessageOut, SessionCreate, SessionOut, SessionUpdate
from app.services import export as export_service

logger = logging.getLogger("kbqa.api.sessions")

router = APIRouter(prefix="/sessions", tags=["会话"])

# 单次导出最多导多少条消息。
#
# 为什么要有上限：一封会话理论上可以有无数条消息，
# 全部读进内存再生成 Excel 可能把服务打爆。
# 2000 条对正常使用完全够（一天问 50 次也要 40 天）。
_EXPORT_MAX_MESSAGES = 2000


async def _own_session(db: DbSession, session_id: int, user_id: int) -> ChatSession:
    """取当前用户自己的会话，不存在或不属于该用户 → 404（不泄露他人会话存在性）。"""
    s = await db.get(ChatSession, session_id)
    if s is None or s.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return s


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    current_user: CurrentUser,
    db: DbSession,
    keyword: str | None = Query(
        None, max_length=100, description="按消息内容搜索（同时匹配会话标题）"
    ),
):
    """当前用户的会话列表（按最近活跃排序）。

    带 keyword 时按**消息正文**搜索，而不只是标题。
    为什么：会话标题是自动取"问题前 18 字"，时间一长根本认不出来；
    真正好用的是"我记得问过某个词" —— 所以搜正文才有价值。
    命中后返回的是会话（不是消息），前端点进去就能看到完整上下文。
    """
    kw = (keyword or "").strip()

    # 会话 + 各自的消息数（LEFT JOIN 聚合）
    stmt = (
        select(ChatSession, func.count(Message.id).label("msg_count"))
        .outerjoin(Message, Message.session_id == ChatSession.id)
        .where(
            ChatSession.user_id == current_user.id,
            # 只列出当前领域的会话：切到别的领域后，原领域的历史会话不应出现
            ChatSession.domain == get_domain().name,
        )
        .group_by(ChatSession.id)
        .order_by(ChatSession.updated_at.desc())
    )

    # 关键词筛选：标题命中 或 正文命中，都算匹配。
    #
    # 为什么用"子查询找 session_id"而不是给 join 加条件：
    #   给 join 加条件会让 msg_count 变成"命中条数"而不是"会话总消息数"。
    #   用户看到"3 条消息"但这个会话其实有 20 条，只会困惑。
    #   子查询保持计数语义不变，只负责筛选显示哪些会话。
    if kw:
        like = f"%{kw}%"
        matched = select(Message.session_id).where(Message.content.ilike(like))
        stmt = stmt.where(
            or_(
                ChatSession.title.ilike(like),
                ChatSession.id.in_(matched),
            )
        )
    rows = (await db.execute(stmt)).all()
    items: list[dict] = []
    for s, msg_count in rows:
        items.append(
            {
                "id": s.id,
                "title": s.title,
                "kb_scope": s.kb_scope,
                "message_count": msg_count,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
        )
    return items


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(payload: SessionCreate, current_user: CurrentUser, db: DbSession):
    """新建一条会话（标题暂为"新对话"，首轮问答后自动生成）。"""
    # 租户校验：前端传进来的知识库 id 必须先收敛，避免把别家组织的库 id 存进会话。
    # 注意这里特意区分两种语义：
    #   kb_ids 留空  → 存 None，表示"跟着我全部可访问的库走"（将来新建的库也能被检索到）
    #   kb_ids 有值  → 存校验后的具体列表（越权 id 已被剔除）
    kb_scope: list[int] | None = None
    if payload.kb_ids:
        resolved = await resolve_kb_scope(db, current_user, payload.kb_ids)
        if not resolved:
            # 选了却一个都不合法：明确报错，而不是静默退化成"全部库"
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "所选知识库不存在或无权访问")
        kb_scope = resolved

    s = ChatSession(
        user_id=current_user.id,
        title="新对话",
        kb_scope=kb_scope,
        # 记下创建时所属的领域：切到别的领域后，这条会话不再出现
        domain=get_domain().name,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return {"id": s.id, "title": s.title, "kb_scope": s.kb_scope, "message_count": 0,
            "created_at": s.created_at, "updated_at": s.updated_at}


@router.patch("/{session_id}", response_model=SessionOut)
async def update_session(
    session_id: int, payload: SessionUpdate, current_user: CurrentUser, db: DbSession
):
    """修改会话（当前仅标题；如用户不满意自动标题可手动改）。"""
    s = await _own_session(db, session_id, current_user.id)
    if payload.title is not None:
        s.title = payload.title
    await db.commit()
    await db.refresh(s)
    return s


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: int, current_user: CurrentUser, db: DbSession):
    """删除会话（消息级联删除，引用快照随消息一起消失）。"""
    s = await _own_session(db, session_id, current_user.id)
    await db.delete(s)
    await db.commit()


@router.get("/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(
    session_id: int,
    current_user: CurrentUser,
    db: DbSession,
    before: int | None = Query(None, description="翻页游标：上一页最旧消息的 id"),
    limit: int = Query(50, ge=1, le=200),
):
    """分页拉取某会话的历史消息（时间正序，往前翻用 before）。"""
    await _own_session(db, session_id, current_user.id)
    stmt = (
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.id.desc())  # 先取最新的 limit 条
        .limit(limit)
    )
    if before:
        stmt = stmt.where(Message.id < before)
    rows = (await db.execute(stmt)).scalars().all()
    rows.reverse()  # 恢复时间正序，前端按顺序渲染
    return rows


@router.get("/{session_id}/export")
async def export_session(
    session_id: int,
    current_user: CurrentUser,
    db: DbSession,
):
    """把一封会话的全部记录导出成 Excel（本用户自己的会话）。

    ## 为什么是 Excel

        用户导出的实际用途通常是"归档 + 统计"：
        问了多少问题、哪些回答被踩、平均响应时间、引用集中在哪几份文档。
        Markdown/PDF 只适合阅读，做不了这些。

    ## 为什么一次导出全部消息（不分页）

        导出的意义就是"完整备份"。
        分页导出会让用户拿到一堆碎片文件，还得自己拼。

        为了防极端情况（一封会话几万条消息把内存打爆），
        这里设了一个上限：超过 `_EXPORT_MAX_MESSAGES` 只导最近的，
        并在响应头里标明"被截断"。

    ## 权限

        只能导自己的会话（`_own_session` 强校验 session.user_id）。
        管理员也不能导别人的 —— 会话内容属于个人隐私。
    """
    sess = await _own_session(db, session_id, current_user.id)

    rows = (
        await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.id.asc())
            .limit(_EXPORT_MAX_MESSAGES)
        )
    ).scalars().all()

    messages = [
        {
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at,
            "citations": m.citations,
            "feedback": m.feedback,
            "feedback_reason": m.feedback_reason,
            "feedback_note": m.feedback_note,
            "latency_ms": m.latency_ms,
        }
        for m in rows
    ]

    content = export_service.build_session_xlsx(
        session_title=sess.title or f"会话{session_id}",
        messages=messages,
    )
    filename = export_service.suggest_filename(
        sess.title or f"会话{session_id}", session_id
    )

    # 文件名里可能有中文，必须按 RFC 5987 编码，
    # 否则某些浏览器会把中文变成乱码。
    from urllib.parse import quote

    ascii_fallback = f"session_{session_id}_export.xlsx"
    disposition = (
        f"attachment; filename=\"{ascii_fallback}\"; "
        f"filename*=UTF-8''{quote(filename)}"
    )

    logger.info(
        "用户 %s 导出会话 %d（%d 条消息）",
        current_user.id, session_id, len(messages),
    )

    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


@router.post("/{session_id}/messages/{message_id}/feedback", response_model=MessageOut)
async def set_message_feedback(
    session_id: int,
    message_id: int,
    payload: FeedbackRequest,
    current_user: CurrentUser,
    db: DbSession,
):
    """给某条 AI 回答打「赞 / 踩 / 取消」。

    为什么必须逐层校验归属（这是本接口唯一的安全要点）：
        消息表里没有 user_id，只有 session_id。如果只看 message_id 就改，
        任何登录用户把 URL 里的数字换个值，就能给**别人的**回答打标签——
        既污染数据，也等于变相确认了"这条消息存在"。
        所以这里严格走 会话属于我 → 消息属于这个会话 → 且是 AI 回答 三步。

    越权一律返回 404（而不是 403）：403 等于告诉对方"这条消息存在但没权限"。
    """
    # 第 1 步：会话必须是我自己的（复用已有的 _own_session，不存在/非本人 → 404）
    await _own_session(db, session_id, current_user.id)

    # 第 2 步：消息必须挂在**这个**会话下（防止"我的会话 + 别人的消息"这种拼装）
    msg = await db.get(Message, message_id)
    if msg is None or msg.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "消息不存在")

    # 第 3 步：只能给 AI 的回答打分（用户自己的提问没有"回答是否准确"这回事）
    if msg.role != "assistant":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只能对 AI 回答进行评价")

    # 落库：0 表示"取消评价"，存 NULL 而不是存 0，
    # 避免把"没评价"和"评价为 0"混在一起。
    if payload.feedback == 0:
        # 取消评价：三个字段一起清空，不能只清 feedback 留下原因/说明 ——
        # 那会变成"没有评价但有原因"的脏数据，后台统计时对不上。
        msg.feedback = None
        msg.feedback_reason = None
        msg.feedback_note = None
        msg.feedback_at = None
    else:
        msg.feedback = payload.feedback
        # 原因与说明只在"踩"时保存：赞的时候用户没理由填这些，
        # 就算前端传了也丢掉，避免出现"满意 + 抱怨原因"的矛盾记录。
        if payload.feedback == -1:
            msg.feedback_reason = payload.reason or None
            msg.feedback_note = (payload.note or None)
        else:
            msg.feedback_reason = None
            msg.feedback_note = None
        msg.feedback_at = dt.datetime.now(dt.timezone.utc)

    await db.commit()
    await db.refresh(msg)
    return msg
