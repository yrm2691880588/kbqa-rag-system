"""会话管理接口（M3）：每个用户独立的会话列表、创建、改名、删除、历史消息拉取。

数据模型关系：user 1—N session 1—N message。
权限：所有操作仅限**当前登录用户自己的会话**（后端强校验 session.user_id == 当前用户），
管理员也不能看别人的会话。
"""

import datetime as dt
import logging

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentUser, DbSession
from app.core.tenancy import resolve_kb_scope
from app.models.message import Message
from app.models.session import ChatSession
from app.schemas.chat import FeedbackRequest, MessageOut, SessionCreate, SessionOut, SessionUpdate

logger = logging.getLogger("kbqa.api.sessions")

router = APIRouter(prefix="/sessions", tags=["会话"])


async def _own_session(db: DbSession, session_id: int, user_id: int) -> ChatSession:
    """取当前用户自己的会话，不存在或不属于该用户 → 404（不泄露他人会话存在性）。"""
    s = await db.get(ChatSession, session_id)
    if s is None or s.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return s


@router.get("", response_model=list[SessionOut])
async def list_sessions(current_user: CurrentUser, db: DbSession):
    """当前用户的会话列表（按最近活跃排序）。"""
    # 会话 + 各自的消息数（LEFT JOIN 聚合）
    stmt = (
        select(ChatSession, func.count(Message.id).label("msg_count"))
        .outerjoin(Message, Message.session_id == ChatSession.id)
        .where(ChatSession.user_id == current_user.id)
        .group_by(ChatSession.id)
        .order_by(ChatSession.updated_at.desc())
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

    s = ChatSession(user_id=current_user.id, title="新对话", kb_scope=kb_scope)
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
