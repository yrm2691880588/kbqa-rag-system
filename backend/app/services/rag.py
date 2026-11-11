"""RAG 问答核心服务：检索 → 组装提示词 → 流式调 DeepSeek → 产出带引用的回答。

设计要点（对应完整问答体验）：
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

import httpx
from openai import AsyncOpenAI

from app.core.config import settings
from app.services import ai_guard
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
            # 两种超时分开设：
            #   · 总超时给得很宽 —— 长文档总结这类回答本来就要跑很久；
            #   · 建连超时给得短 —— "域名解析不了 / 网络不通 / 防火墙拦了"
            #     这类问题要很快暴露，不能让用户对着转圈等两分钟。
            # 分开设才有意义；只给一个总超时的话，网络不通也要等到总超时才报错。
            timeout=httpx.Timeout(
                float(getattr(settings, "llm_request_timeout", 120.0) or 120.0),
                connect=float(getattr(settings, "llm_connect_timeout", 15.0) or 15.0),
            ),
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


def _mock_llm_answer(question: str, citations: list[dict]) -> str:
    """压测专用：用本地拼接代替 DeepSeek 生成回答（不联网、不花钱）。

    仅当 settings.llm_mock=True 时被调用。回答内容基于检索命中片段拼一段
    结构化的假答案，保证 SSE 流式链路（分片、引用角标、done 事件）完整走通。
    """
    lines = [f"根据知识库资料，关于「{question[:30]}」的模拟回答如下：", ""]
    for c in citations:
        snippet = (c.get("text") or "").strip().replace("\n", " ")[:60]
        lines.append(f"[{c['index']}] {snippet}（来源：{c.get('doc_name', '')}）")
    lines.append("")
    lines.append("（以上为压测模式下的本地模拟回答，未调用真实大模型。）")
    return "\n".join(lines)


async def _stream_mock_answer(question: str, citations: list[dict]):
    """压测专用：把模拟回答按小片逐段 yield，模拟真实流式输出节奏。

    Yields 与真实回答相同的事件结构：
    ("thinking", ...) → 多个 ("answer", ...) 增量 → ("done", ...)
    """
    yield ("thinking", {"text": "正在检索知识库并组织回答…"})
    mock_text = _mock_llm_answer(question, citations)
    # 按 8 字左右一小片切分，模拟真实 token 流（让前端打字机效果与 SSE 压力真实）
    step = 8
    for i in range(0, len(mock_text), step):
        yield ("answer", {"text": mock_text[i : i + step]})


def _remember(cache_key: str, answer: str, citations: list) -> None:
    """一次问答结束后的收尾：更新熔断状态，并按需写入问题缓存。

    ⚠️ 为什么要抽成函数：真实调用和模拟模式（压测/本地开发）是**两条不同的代码路径**。
        如果只在其中一条里写缓存，就会出现"本地测着缓存好使、上线完全没生效"——
        这种问题在开发时根本发现不了。抽成一处，两条路都走它，就不会漏。
    """
    if answer.strip():
        # "成功"的定义要卡严：**真的产出了回答文字**才算成功。
        # 如果模型返回空内容也算成功，熔断器就永远跳不了闸 ——
        # 上游"半死不活"（每次都 200 但内容为空）时，熔断形同虚设。
        ai_guard.llm_breaker.record_success()
        # 只有"真答出来了"才写缓存：空回答写进去，下次命中就是给用户一个空气泡
        if cache_key:
            ai_guard.question_cache.put(cache_key, {"answer": answer, "citations": citations})
    else:
        ai_guard.llm_breaker.record_failure()


async def stream_answer(
    question: str,
    history: list[dict],
    kb_ids: list[int] | None = None,
    client: AsyncOpenAI | None = None,
    cache_scope: str = "",
):
    """流式问答生成器：yield (event, data) 元组，由 API 层逐条转成 SSE 发往前端。

    Args:
        question: 用户当前问题。
        history: 最近多轮历史 [{"role": "user"|"assistant", "content": "..."}, ...]。
        kb_ids: 限定检索的知识库 id 列表；None=全部。
        client: 测试时可注入 mock 客户端。
        cache_scope: 缓存隔离键。调用方传"是谁在问"（例如组织 id）。
            **不传就不使用缓存** —— 这个默认值是刻意安全的：
            万一调用方忘了传，后果是"没缓存、多花点钱"，而不是"串了别人的答案"。

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

    # 0.5) 问题缓存：同样的问法直接返回上次的答案，省一次检索 + 一次模型调用。
    #
    #     三条"什么时候才敢用缓存"的判断，缺一不可：
    #       ① 调用方给了隔离键 —— 否则可能把别人的答案发给当前用户；
    #       ② **没有多轮历史** —— 追问（"那它呢？"）的含义完全取决于上下文，
    #          只按问题原文缓存会答错；
    #       ③ 配置里确实开了 —— 默认是关的，因为知识库刚更新时缓存会答"老话"。
    cache_key = ""
    if cache_scope and not history:
        cache_key = ai_guard.make_cache_key(question, kb_ids, cache_scope)
        cached = ai_guard.question_cache.get(cache_key)
        if cached is not None:
            # 命中缓存：把上次的引用清单和整段回答原样"重放"一遍。
            # 前端拿到的事件结构与真实回答完全一致，它不需要知道这是缓存。
            yield ("meta", {"citations": cached.get("citations", []), "cached": True})
            if cached.get("answer"):
                yield ("answer", {"text": cached["answer"]})
            yield ("done", {
                "latency_ms": int((time.time() - started) * 1000),
                "citations": cached.get("citations", []),
                "has_answer": bool(cached.get("answer")),
                "cached": True,
                "tokens_in": 0,
                "tokens_out": 0,
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

        # 3) 组装消息，流式调模型（压测模式下走本地模拟流，不联网不花钱）
        if settings.llm_mock:
            mock_parts: list[str] = []
            async for evt, data in _stream_mock_answer(question, citations):
                if evt == "answer":
                    mock_parts.append(data.get("text", ""))
                yield evt, data
            mock_answer = "".join(mock_parts)
            _remember(cache_key, mock_answer, citations)
            yield ("done", {
                "latency_ms": int((time.time() - started) * 1000),
                "citations": citations,
                "has_answer": True,
                "mock": True,
            })
            return

        messages = build_messages(question, history, citations)
        llm = client or _get_llm_client()

        # 熔断闸门：外部服务连续失败到阈值后，这里直接快速失败。
        # 为什么要"快速失败"：外部接口坏了的时候，每个请求都干等 120 秒才报错，
        # 工作线程会迅速被占满 —— 结果是"大模型挂了"变成"我们网站也打不开了"。
        # 跳闸期间立刻回一句"稍后再试"，至少网站本身还能正常打开。
        if not ai_guard.llm_breaker.allow():
            logger.warning("熔断器处于跳闸状态，本次请求快速失败（不调用大模型）")
            yield ("error", {
                "message": "问答服务暂时繁忙，请稍后重试。",
                "reason": "circuit_open",
            })
            yield ("done", {
                "latency_ms": int((time.time() - started) * 1000),
                "citations": citations,
                "has_answer": False,
            })
            return
        # 如果在试探状态，先占一个试探名额（避免冷却一结束就涌进一堆请求）
        ai_guard.llm_breaker.record_trial()

        call_kwargs = dict(
            model=settings.llm_model,
            messages=messages,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            stream=True,
        )
        # 尽量把 token 用量一起取回来（用于额度计量）。
        # 这个参数不是所有兼容网关都认，不认识会直接报错——所以失败就退回老写法：
        # **宁可拿不到用量数字，也不能让问答本身用不了**。
        try:
            if settings.llm_stream_usage:
                try:
                    stream = await llm.chat.completions.create(
                        **call_kwargs, stream_options={"include_usage": True}
                    )
                except Exception:
                    logger.warning("上游不支持 stream_options（拿不到 token 用量），本次按普通模式继续")
                    stream = await llm.chat.completions.create(**call_kwargs)
            else:
                stream = await llm.chat.completions.create(**call_kwargs)
        except Exception:
            # 连"发起请求"这一步都没成功（网络不通、密钥失效、被限流）→ 记一次熔断失败
            ai_guard.llm_breaker.record_failure()
            raise

        # 4) 逐块转发：思考字段与回答字段各自增量输出
        answer_parts: list[str] = []
        usage = None
        async for chunk in stream:
            # 用量一般挂在最后一个 chunk 上（choices 为空、usage 有值）
            u = getattr(chunk, "usage", None)
            if u is not None:
                usage = u
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
        _remember(cache_key, full_answer, citations)

        yield ("done", {
            "latency_ms": int((time.time() - started) * 1000),
            "citations": citations,
            "has_answer": bool(full_answer.strip()),
            "tokens_in": getattr(usage, "prompt_tokens", 0) or 0,
            "tokens_out": getattr(usage, "completion_tokens", 0) or 0,
        })
    except Exception as e:
        logger.exception("问答流程异常")
        # 记一次熔断失败：这是"外部服务可能真的挂了"最直接的信号
        ai_guard.llm_breaker.record_failure()
        # ⚠️ 只给用户一句通用提示，**不能**把 str(e) 透传出去。
        # 原因：上游 SDK 的异常文本里常带着 api_key 片段、request_id、
        # 内部域名、甚至上游文档链接（实测阿里云 401 会带一长串）。
        # 把这些原样吐给前端，等于把后端配置和供应商信息告诉攻击者。
        # 真正的排查信息留在日志里（logger.exception 已经记了完整栈）。
        yield ("error", {"message": "回答生成失败，请稍后重试；如持续失败请联系管理员"})
        yield ("done", {"latency_ms": int((time.time() - started) * 1000), "citations": []})
