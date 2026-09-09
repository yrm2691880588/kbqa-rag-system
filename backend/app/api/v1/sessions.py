"""会话管理接口（M3）：每个用户独立的会话列表、创建、改名、删除、历史消息拉取。

数据模型关系：user 1—N session 1—N message。
权限：所有操作仅限**当前登录用户自己的会话**（后端强校验 session.user_id == 当前用户），
管理员也不能看别人的会话。
"""

import logging

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentUser, DbSession
from app.models.message import Message
from app.models.session import ChatSession
from app.schemas.chat import MessageOut, SessionCreate, SessionOut, SessionUpdate

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
    s = ChatSession(user_id=current_user.id, title="新对话", kb_scope=payload.kb_ids)
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
