"""RAG 问答核心服务：检索 → 组装提示词 → 流式调 DeepSeek → 产出带引用的回答。

设计要点（对应企业级问答体验）：
1. **检索即引用**：用户问题先向量检索知识库，命中片段按 [1][2]… 编号注入提示词；
   回答正文里的角标与"引用列表"一一对应，前端可点开看原文。
2. **不硬答**：检索无命中 / 全部低于相似度阈值时，明确返回"未找到相关资料"，
   不把问题丢给模型瞎编（这是 RAG 防止幻觉的关键防线）。
3. **流式双阶段**：DeepSeek v4-flash 是推理模型，响应分两段：
   reasoning_content（内部思考）→ content（正式回答）。我们逐字转发给前端，
   前端可先显示"思考中"再逐字输出回答，体验接近真实 AI 产品。
4. **多轮上下文**：携带最近 N 轮问答历史（user/assistant 交替）辅助理解追问，
   但历史只作参考，答案仍以本次检索片段为准。
"""

import json
import logging
import time

from openai import AsyncOpenAI

from app.core.config import settings
from app.services.retriever import retrieve
from app.services.smalltalk import detect_smalltalk

logger = logging.getLogger("kbqa.rag")

_llm_client: AsyncOpenAI | None = None


def _get_llm_client() -> AsyncOpenAI:
    """惰性创建 DeepSeek 对话客户端（async）。"""
    global _llm_client
    if _llm_client is None:
        _llm_client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=120,
            max_retries=0,  # 流式中断由调用方捕获处理，不自动重试
        )
    return _llm_client


def hit_to_citation(idx: int, hit: dict) -> dict:
    """把一条 Qdrant 命中转成引用条目（带顺序号、来源名、页码、原文、相似度）。"""
    return {
        "index": idx,                       # 回答正文角标序号（从 1 起）
        "doc_name": hit.get("doc_name", ""),
        "page": hit.get("page"),
        "text": hit.get("text", ""),
        "score": hit.get("score", 0.0),
        "kb_id": hit.get("kb_id"),
    }


def build_system_prompt(citations: list[dict]) -> str:
    """构造系统提示词：把引用片段按编号嵌入，并规定回答纪律。"""
    if not citations:
        return (
            "你是一个电商商品知识库问答助手。当前知识库中未检索到与问题相关的资料。"
            "请礼貌地告诉用户：在现有知识库中没有找到相关资料，并建议换个问法或联系人工客服。"
            "不要编造答案，不要使用任何知识库之外的商品信息。"
        )
    parts = []
    parts.append("你是电商商品知识库问答助手。请严格依据下面提供的【本次知识库资料片段】回答用户**当前这个问题**：")
    parts.append("")
    for c in citations:
        src = f"《{c['doc_name']}》" + (f"（第 {c['page']} 页）" if c.get("page") else "")
        parts.append(f"[{c['index']}] 来源：{src}")
        parts.append(c["text"])
        parts.append("")
    parts.append("回答规则：")
    parts.append("1. 优先使用上面片段中的信息作答；片段没提到的，不要猜测或编造。")
    parts.append("2. 回答中引用到哪段资料，就在对应句子末尾标注角标，如 [1][2]，与片段编号一致。")
    parts.append("3. 如果片段不足以完整回答问题，如实说明，并提示用户查看《资料文件名》或咨询人工客服。")
    parts.append("4. 用简体中文、清晰的分点/表格组织答案，面向普通消费者，专业术语附简短解释。")
    parts.append("5. 对话历史仅用于理解当前问题中的指代（如“它/那款/这个”指哪个商品）；不要回顾、复述或评价历史话题，更不要提及“历史问题中某某资料没找到”之类与当前问题无关的内容。")
    parts.append("6. 只针对用户当前这一问作答：历史里问过的其他商品，除非当前问题明确要求对比，否则不要展开说明。")
    return "\n".join(parts)


def build_messages(question: str, history: list[dict], citations: list[dict]) -> list[dict]:
    """组装发给模型的完整对话：系统提示 + 最近 N 轮历史 + 当前问题。"""
    system_prompt = build_system_prompt(citations)
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    # 历史只保留最近 N 轮（user/assistant 交替），控制 token 窗口
    for m in history[-settings.llm_max_history_rounds * 2 :]:
        if m.get("role") in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m.get("content", "")})
    messages.append({"role": "user", "content": question})
    return messages


async def stream_answer(
    question: str,
    history: list[dict],
    kb_ids: list[int] | None = None,
    client: AsyncOpenAI | None = None,
):
    """流式问答生成器：yield (event, data) 元组，由 API 层逐条转成 SSE 发往前端。

    Args:
        question: 用户当前问题。
        history: 最近多轮历史 [{"role": "user"|"assistant", "content": "..."}, ...]。
        kb_ids: 限定检索的知识库 id 列表；None=全部。
        client: 测试时可注入 mock 客户端。

    Yields:
        (event, data) 事件对，事件类型：
        - ("meta", {"citations": [...]})     检索命中的引用列表
        - ("thinking", {"text": 增量})       模型思考过程增量
        - ("answer", {"text": 增量})         正式回答增量
        - ("no_hit", {"message": str})       未检索到资料
        - ("done", {"latency_ms": int, "citations": [...]})  正常结束
        - ("error", {"message": str})        出错
    """
    started = time.time()

    # 0) 寒暄识别：问候/致谢/自我介绍等不查知识库，直接礼貌回应
    smalltalk_reply = detect_smalltalk(question)
    if smalltalk_reply:
        yield ("answer", {"text": smalltalk_reply})
        yield ("done", {
            "latency_ms": int((time.time() - started) * 1000),
            "citations": [],
            "has_answer": True,
        })
        return

    try:
        # 1) 检索知识库（retriever 内部已完成文档级去噪与截断）
        hits = await retrieve(question, kb_ids=kb_ids, top_k=settings.retrieval_top_k)
        citations = [hit_to_citation(i + 1, h) for i, h in enumerate(hits)]

        if not citations:
            # 无命中 → 不硬答，直接告诉用户没找到
            yield ("no_hit", {
                "message": "我在知识库里没有找到与这个问题相关的资料。您可以换个问法，或让管理员补充相关资料后再试。",
                "citations": [],
            })
            yield ("done", {"latency_ms": int((time.time() - started) * 1000), "citations": []})
            return

        # 2) 有命中 → 先发引用清单（前端据此渲染角标 & 引用抽屉）
        yield ("meta", {"citations": citations})

        # 3) 组装消息，流式调模型
        messages = build_messages(question, history, citations)
        llm = client or _get_llm_client()
        stream = await llm.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            stream=True,
        )

        # 4) 逐块转发：思考字段与回答字段各自增量输出
        answer_parts: list[str] = []
        async for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            thinking = getattr(delta, "reasoning_content", None)
            content = getattr(delta, "content", None)
            if thinking:
                yield ("thinking", {"text": thinking})
            if content:
                answer_parts.append(content)
                yield ("answer", {"text": content})

        # 5) 结束
        full_answer = "".join(answer_parts)
        yield ("done", {
            "latency_ms": int((time.time() - started) * 1000),
            "citations": citations,
            "has_answer": bool(full_answer.strip()),
        })
    except Exception as e:
        logger.exception("问答流程异常")
        yield ("error", {"message": f"回答生成失败：{str(e)[:300]}"})
        yield ("done", {"latency_ms": int((time.time() - started) * 1000), "citations": []})
