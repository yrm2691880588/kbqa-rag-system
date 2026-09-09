"""问答接口（M3 核心）：POST /chat 以 SSE 流式返回回答，回答带引用片段。

流程：
1. 校验会话归属 → 读该会话历史 → 存下用户提问消息；
2. 首轮提问且会话仍叫"新对话"时，自动用问题前几字生成标题；
3. **开放性意图短路**：若问题被识别为"列举/总览"类（如"介绍一下本商品""有哪些商品"），
   直接从数据库读知识库与文档清单主动列给用户，避免生硬的"未找到资料"；
4. 否则调用 rag.stream_answer 流式生成，逐事件转发给前端（meta/thinking/answer/no_hit/done/error）；
5. 流结束后把 assistant 回答（含引用快照）写入消息表，并记录问答日志（管理员洞察用）。

实现注意：流式生成期间**不能持有请求注入的数据库会话**（响应流结束后依赖才清理，
而生成器要在流中途写库），因此写库统一在生成器内部用独立会话完成。
"""

import datetime as dt
import json
import logging
import time

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession
from app.db.session import AsyncSessionLocal
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.message import Message
from app.models.query_log import QueryLog
from app.models.session import ChatSession
from app.schemas.chat import ChatRequest
from app.services.open_intent import detect_open_intent
from app.services.rag import stream_answer

logger = logging.getLogger("kbqa.api.chat")

router = APIRouter(prefix="/chat", tags=["问答"])


def _sse(event: str, data: dict) -> str:
    """把事件序列化成一行 SSE 文本（event + data 两行，空行结束）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("")
async def chat(payload: ChatRequest, current_user: CurrentUser, db: DbSession):
    """发起一轮知识库问答，SSE 流式返回（前端 fetch ReadableStream 消费）。"""
    # 1) 会话归属校验 + 读历史
    s = await db.get(ChatSession, payload.session_id)
    if s is None or s.user_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")

    hist_rows = (
        (await db.execute(
            select(Message).where(Message.session_id == s.id).order_by(Message.id)
        ))
        .scalars()
        .all()
    )
    history = [{"role": m.role, "content": m.content} for m in hist_rows]

    # 2) 存用户消息；若首轮且未改名，自动取问题摘要做标题；刷新会话活跃时间
    db.add(Message(session_id=s.id, role="user", content=payload.question))
    if s.title == "新对话":
        s.title = payload.question[:18] + ("…" if len(payload.question) > 18 else "")
    s.updated_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()

    kb_ids = s.kb_scope
    user_id = current_user.id
    session_id = s.id
    question = payload.question

    async def event_gen():
        """SSE 事件生成器：转发模型流 + 结束后独立会话写库。"""
        # 流结束后用于写库的字段
        answer_buf: list[str] = []
        citations: list = []
        answered = False
        started = time.time()

        async with AsyncSessionLocal() as adb:
            try:
                # 0) 开放性意图短路：用户问"介绍一下本商品/有哪些商品"时，直接从 DB 列清单，
                #    避免生硬的"未找到资料"。
                if detect_open_intent(question):
                    text = await _build_guided_listing(adb, kb_ids)
                    answer_buf.append(text)
                    answered = True
                    yield _sse("answer", {"text": text})
                    yield _sse("done", {
                        "latency_ms": int((time.time() - started) * 1000),
                        "citations": [],
                        "has_answer": True,
                        "guided": True,  # 标记由智能引导给出,前端可选用
                    })
                else:
                    async for event, data in stream_answer(question, history, kb_ids):
                        if event == "meta":
                            citations = data.get("citations", [])
                            yield _sse("meta", data)
                        elif event in ("thinking", "answer"):
                            if event == "answer":
                                answer_buf.append(data.get("text", ""))
                            yield _sse(event, data)
                        elif event == "no_hit":
                            answered = False
                            # 未命中也要落一条 assistant 消息，让历史完整可续聊
                            answer_buf.append(data.get("message", "知识库中未找到相关资料。"))
                            yield _sse("no_hit", data)
                        elif event == "done":
                            answered = bool(data.get("has_answer"))
                            latency = data.get("latency_ms", 0)
                            yield _sse("done", data)
                        elif event == "error":
                            answer_buf.append(data.get("message", "生成失败，请重试。"))
                            yield _sse("error", data)
                            yield _sse("done", {"latency_ms": int((time.time() - started) * 1000), "citations": citations})

                # 3) 流结束：落 assistant 消息 + 刷新会话活跃时间 + 问答日志
                full_answer = "".join(answer_buf).strip()
                if full_answer:
                    adb.add(
                        Message(
                            session_id=session_id,
                            role="assistant",
                            content=full_answer,
                            citations=citations or None,
                            latency_ms=int((time.time() - started) * 1000),
                        )
                    )
                sess = await adb.get(ChatSession, session_id)
                if sess:
                    sess.updated_at = dt.datetime.now(dt.timezone.utc)
                adb.add(
                    QueryLog(
                        user_id=user_id,
                        session_id=session_id,
                        question=question,
                        kb_scope=kb_ids,
                        retrieval=citations or None,
                        answered=answered,
                        latency_ms=int((time.time() - started) * 1000),
                    )
                )
                await adb.commit()
            except Exception:
                logger.exception("问答流处理异常")
                yield _sse("error", {"message": "服务内部错误，请稍后重试"})
                yield _sse("done", {"latency_ms": int((time.time() - started) * 1000), "citations": []})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 让 nginx 类反代不缓冲流
        },
    )


async def _build_guided_listing(db, kb_ids: list[int] | None) -> str:
    """开放性问题引导：列出当前可用的知识库与各库内的 ready 文档名，供用户挑着问。

    Args:
        db: 写库用会话（在 event_gen 内由 AsyncSessionLocal 提供）。
        kb_ids: 会话的检索范围；None 视作全部启用库。

    Returns:
        引导文案（中文多行），前端直接作为 AI 消息展示。
    """
    base_kb = select(KnowledgeBase).where(KnowledgeBase.is_active.is_(True))
    if kb_ids:
        base_kb = base_kb.where(KnowledgeBase.id.in_(kb_ids))
    kbs = (await db.execute(base_kb.order_by(KnowledgeBase.created_at.desc()))).scalars().all()

    if not kbs:
        return (
            "目前系统里还没有可用的知识库。"
            "你可以请管理员先去「知识库管理」里创建一个，"
            "然后把商品资料上传进去，再回来提问效果会更好。"
        )

    lines: list[str] = []
    lines.append(
        "您好！您的问题比较宽泛，系统里没有能直接回答的单一文档。"
        "目前知识库里已收录以下内容，您可以挑一个具体的问题来问，例如：\n"
        "  • 「A8 适用多大面积」「S10 电池容量是多少」「耳机支持防水吗」之类。\n"
    )
    for kb in kbs:
        kb_title = f"【{kb.name}】"
        if kb.description:
            kb_title += f" — {kb.description}"
        lines.append(kb_title)
        doc_rows = (await db.execute(
            select(Document)
            .where(Document.kb_id == kb.id, Document.status == "ready")
            .order_by(Document.created_at.desc())
        )).scalars().all()
        if not doc_rows:
            lines.append("  · （暂无文档）")
        else:
            for d in doc_rows:
                lines.append(f"  · {d.filename}")
        lines.append("")
    lines.append("告诉我您想先了解哪款，我就帮您在知识库里找到答案并标上引用。")
    return "\n".join(lines)
