"""对外问答接口（P1-G）：企业用 API Key 调用，把问答接进自己的系统。

与网页端问答的三点区别（都是有意的）：
    ① **非流式**：一次返回完整 JSON。
       网页要打字机效果是因为有"人在盯着屏幕等"；后端到后端的对接，
       拿到完整对象更好处理（尤其是要再往自家系统里塞的场景）。
    ② **无会话**：不落库、不建会话记录。
       对接方自己管理上下文，需要多轮就把历史一起传进来（history 字段）。
       好处是平台不会替客户攒一堆他们不要的聊天记录。
    ③ **额度与内容安全照样生效**：
       走 API 不等于绕过管控——额度按组织算，敏感词照样拦。
       否则"API 通道"就成了刷额度、发违规内容的绕道，那就白做了。

隔离：所有检索范围都经过 core/tenancy.resolve_kb_scope_for_org 收敛，
      企业 A 的 Key 无论传什么 id，都拿不到企业 B 的知识库。
"""

import logging
import time

from fastapi import APIRouter, HTTPException, status

from app.core.api_key_auth import ApiClientDep
from app.core.config import settings
from app.core.deps import DbSession
from app.core.tenancy import resolve_kb_scope_for_org
from app.schemas.api_key import OpenChatRequest, OpenChatResponse, OpenCitation
from app.services import content_filter, quota
from app.services.open_intent import detect_open_intent
from app.services.rag import stream_answer

logger = logging.getLogger("kbqa.api.open")

router = APIRouter(prefix="/open", tags=["对外 API"])

# 命中内容安全策略时的统一话术（与网页端保持一致，避免两边口径不同）
_BLOCKED_ANSWER = (
    "抱歉，这条回答涉及不适宜的内容，已被系统安全策略拦截。"
    "您可以换一种问法，或联系管理员确认知识库里的资料是否需要调整。"
)


@router.post("/chat", response_model=OpenChatResponse)
async def open_chat(payload: OpenChatRequest, client: ApiClientDep, db: DbSession):
    """对外问答：一个 HTTP 请求拿一个完整回答（含引用来源）。"""
    started = time.time()
    question = payload.question

    # ---- 1) 内容安全：先审用户提问（和网页端同一套词库）----
    if settings.content_filter_enabled and settings.content_filter_block_question:
        q_hit = content_filter.check(question, mask=False)
        if q_hit.hit:
            logger.warning("API 调用（组织 %s）的提问命中敏感词，已拦截", client.org_id)
            cats = "、".join(q_hit.categories) or "敏感"
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"问题包含不适宜的内容（涉及「{cats}」），请修改后重试",
            )

    # ---- 2) 额度闸门：和网页端共用同一份组织额度 ----
    decision = await quota.check_and_consume_question(db, client.org_id)
    if not decision.allowed:
        logger.info("API 调用触发额度限制（组织 %s）：%s", client.org_id, decision.reason)
        raise HTTPException(decision.status_code, decision.reason)

    # ---- 3) 隔离收敛：把请求里的 kb_ids 收敛成"这个组织真正可用的" ----
    kb_ids = await resolve_kb_scope_for_org(db, client.org_id, payload.kb_ids)
    if not kb_ids:
        # 一个库都没有：没必要去调大模型（那会白白花钱），直接给出明确答复
        return OpenChatResponse(
            answer="该账号下还没有可用的知识库。请先在后台创建知识库并上传资料，再来提问。",
            citations=[], has_answer=False, blocked=False,
            latency_ms=int((time.time() - started) * 1000),
            remaining_today=_remaining(decision),
        )

    # ---- 4) 生成回答 ----
    citations: list[OpenCitation] = []
    answer_parts: list[str] = []
    answered = False
    blocked = False
    guard = content_filter.StreamGuard()
    tokens_in = 0
    tokens_out = 0

    # 历史：只透传角色与内容，忽略调用方塞的其它字段
    history = []
    for item in payload.history or []:
        if isinstance(item, dict) and item.get("role") in ("user", "assistant") and item.get("content"):
            history.append({"role": item["role"], "content": str(item["content"])[:4000]})

    if detect_open_intent(question):
        # "有哪些资料"这类宽泛问题：直接列清单，别生硬地说"没找到"
        from app.api.v1.chat import _build_guided_listing
        text = await _build_guided_listing(db, kb_ids, client.org_id)
        if settings.content_filter_enabled and content_filter.check(text, mask=False).hit:
            text, blocked = _BLOCKED_ANSWER, True
        answer_parts.append(text)
        answered = not blocked
    else:
        try:
            async for event, data in stream_answer(
                question, history, kb_ids, cache_scope=f"org:{client.org_id}"
            ):
                if event == "meta":
                    citations = [OpenCitation(**c) for c in (data.get("citations") or [])]
                elif event == "answer":
                    piece = data.get("text", "")
                    if settings.content_filter_enabled:
                        hit = guard.feed(piece)
                        if hit.hit:
                            blocked = True
                            full = "".join(answer_parts) + piece
                            logger.warning("API 回答命中敏感词（组织 %s），已掐断", client.org_id)
                            answer_parts = [
                                content_filter.mask(full)
                                if (settings.content_filter_answer_mode or "").lower() == "mask"
                                else _BLOCKED_ANSWER
                            ]
                            answered = False
                            break
                    answer_parts.append(piece)
                elif event == "no_hit":
                    answered = False
                    answer_parts.append(data.get("message", "知识库中未找到相关资料。"))
                elif event == "done":
                    answered = bool(data.get("has_answer")) and not blocked
                    tokens_in = int(data.get("tokens_in", 0) or 0)
                    tokens_out = int(data.get("tokens_out", 0) or 0)
                elif event == "error":
                    answer_parts.append(data.get("message", "生成失败，请重试。"))
        except Exception:
            logger.exception("对外问答生成异常（组织 %s）", client.org_id)
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "回答生成失败，请稍后重试")

    answer = "".join(answer_parts).strip()

    # ---- 5) 记账 token（提问次数在第 2 步已经扣过了）----
    # 这里用**请求自带的那条数据库会话**，而不是另开一条：
    #   本接口是非流式的，函数返回前会话一直有效，没有"流到一半会话被关掉"的问题。
    #   另开一条会话会带来两个麻烦：多占一个数据库连接；以及验证脚本没法把数据库
    #   重定向到临时库（依赖注入换不掉它），结果测试会悄悄写到正式库上去。
    if tokens_in or tokens_out:
        await quota.add_llm_tokens(db, client.org_id, tokens_in, tokens_out)

    return OpenChatResponse(
        answer=answer,
        citations=citations,
        has_answer=answered,
        blocked=blocked,
        latency_ms=int((time.time() - started) * 1000),
        remaining_today=_remaining(decision),
    )


def _remaining(decision) -> int | None:
    """今日还剩几次（不限量时返回 None）。

    为什么要返回这个：对接方可以据此做自己的限流/告警，
    而不是等被拦了才发现"额度用完了"。
    """
    if decision.daily_limit is None:
        return None
    return max(0, decision.daily_limit - decision.daily_used)
