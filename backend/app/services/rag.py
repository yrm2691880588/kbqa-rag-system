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
from app.domain import get_domain
from app.services import ai_guard
from app.services.answer_cleanup import strip_thinking_safe
from app.services.answer_verify import build_retry_hint, verify_answer
from app.services.intent_router import Intent, route
from app.tools.registry import execute_tool, get_schemas, get_tools_for
from app.services.query_rewrite import rewrite_query
from app.services.retriever import retrieve

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
    domain = get_domain()

    if not citations:
        # 无命中提示语由领域提供：不同领域对「没找到资料」的措辞不同。
        # 笼统的"没找到资料"会让用户以为系统坏了。
        msg = (
            f"{domain.persona}。当前知识库中未检索到与问题相关的资料。"
            f"请礼貌地告诉用户：{domain.no_hit_message}，并建议换个问法或联系人工客服。"
        )
        # 禁令是给模型的约束，不是给用户的话术，所以放在提示语的指令部分，
        # 并显式标明"这是给你的要求"，避免模型把它念给用户听。
        if domain.forbidden:
            constraints = "；".join(domain.forbidden)
            msg += f"\n（以下是对你的要求，不要念给用户：{constraints}）"
        return msg
    parts = []
    # 身份 + 受众来自领域：换领域只改领域模块，这里一个字不用动
    parts.append(
        f"{domain.persona}。请严格依据下面提供的【本次知识库资料片段】"
        f"回答用户**当前这个问题**："
    )
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
    parts.append(
        f"4. 用简体中文、清晰的分点/表格组织答案，面向{domain.audience}，专业术语附简短解释。"
    )
    # 规则 5/6 讲的是"怎么处理对话历史"，属于系统正确性要求，与领域无关。
    # 用中性的「哪个对象」，换领域后依然成立。
    parts.append(
        "5. 对话历史仅用于理解当前问题中的指代（如“它/那款/这个”指哪个对象）；"
        "不要回顾、复述或评价历史话题，更不要提及“历史问题中某某资料没找到”之类与当前问题无关的内容。"
    )
    parts.append(
        "6. 只针对用户当前这一问作答：历史里问过的其他对象，"
        "除非当前问题明确要求对比，否则不要展开说明。"
    )
    # 实测发现：部分模型会把"分析过程"直接写进正文，例如
    #   "用户问某某指标。资料显示该指标为某个数值。回答简洁，标注[1]。"
    # 然后才给正式答案。用户第一屏看到的全是内部独白，体验很差。
    parts.append(
        "7. **直接给出答案**，不要输出分析过程、不要复述用户的提问、"
        "不要写「用户问…」「资料显示…」「回答应简洁」这类内部独白。"
        "第一句话就应该是给用户看的正式内容。"
    )
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


async def _run_tool_loop(
    messages: list[dict],
    tools: list,
    llm: AsyncOpenAI,
    max_rounds: int,
) -> list[dict]:
    """执行工具调用循环，返回可以继续用来生成最终答案的 messages。

    ## 流程

        第 1 轮：把工具定义交给模型
              ↓ 模型返回 tool_calls？
             否 → 直接把这一轮的回复当答案（它决定不用工具）
             是 → 执行工具，把结果追加进 messages
        第 2 轮：带着工具结果再问模型
              ↓ 还有 tool_calls？
             有且未超上限 → 继续
             没有 → 结束，交给外层流式生成最终答案

    ## 为什么返回 messages 而不是直接返回答案

        最终答案是**流式**生成的（边生成边显示）。如果这里也生成一遍，
        等于生成两次：一次非流式（这里）、一次流式（外层）——
        既浪费钱，又可能出现两个版本的答案。

        所以这里只负责"把工具执行结果补进对话"，
        最后让外层用补好的 messages 走流式生成。

    ## 失败处理

        工具执行失败不抛异常：把错误文本交给模型，它会决定
        是换个参数重试、还是用现有信息回答。
        整个循环异常也不抛 —— 返回原始 messages，让问答照常进行。
    """
    if max_rounds <= 0 or not tools:
        return messages

    schemas = get_schemas(tools)
    working = list(messages)

    for round_idx in range(max_rounds):
        try:
            resp = await llm.chat.completions.create(
                model=settings.llm_model,
                messages=working,
                tools=schemas,
                tool_choice="auto",   # 由模型自己决定要不要用
                temperature=0,        # 工具调用要确定性，避免来回摇摆
                max_tokens=settings.llm_max_tokens,
                stream=False,         # 中间轮次不需要流式
            )
        except Exception as exc:  # noqa: BLE001
            # 上游不支持 tools 或临时故障：放弃工具，按普通问答继续
            logger.warning("工具调用第 %d 轮失败，跳过工具：%s", round_idx + 1, exc)
            return messages

        if not resp.choices:
            return working

        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)

        # 模型没用工具 → 它认为不需要，结束循环
        if not tool_calls:
            return working

        # 把模型的"我要调用工具"这条消息也加进历史
        # （API 要求 tool 消息之前必须有对应的 assistant tool_calls 消息）
        working.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        # 逐个执行工具
        for tc in tool_calls:
            name = tc.function.name
            raw_args = tc.function.arguments or "{}"

            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}

            logger.info("工具调用：%s(%s)", name, raw_args[:120])
            result_text = await execute_tool(tools, name, args)

            working.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text,
                }
            )

    # 用完轮数仍有 tool_calls：不再继续，让外层用现有信息作答。
    # 追加一句提示，告诉模型"不要继续调工具了"。
    working.append(
        {
            "role": "system",
            "content": (
                "工具调用次数已达上限。请基于目前已获得的信息直接给出最终答案，"
                "不要再请求调用工具。如果信息不足，就说明哪些信息不足。"
            ),
        }
    )
    return working


async def _regenerate_with_hint(
    question: str,
    citations: list[dict],
    history: list[dict],
    hint: str,
    client: AsyncOpenAI | None = None,
) -> str:
    """带着"上一版哪里不对"的提示，重新生成一次答案。

    为什么单独成函数：
        重做和首次生成的提示词不同（多了一段修正要求），
        逻辑独立、便于单测，也便于将来调整策略。

    Returns:
        新答案文本；失败返回空字符串（调用方据此走"无法可靠回答"分支）。
    """
    try:
        llm = client or _get_llm_client()
        system = build_system_prompt(citations) + hint
        messages = [{"role": "system", "content": system}]
        for m in history[-settings.llm_max_history_rounds * 2 :]:
            messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": question})

        resp = await llm.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            temperature=0.2,  # 比重做前略低，减少发散
            max_tokens=settings.llm_max_tokens,
            stream=False,     # 重做不需要流式：反正要等自检
        )
        if resp.choices:
            return (resp.choices[0].message.content or "").strip()
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("重做失败：%s", exc)
        return ""


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

    # 0) 意图分流：先判断这个问题该怎么处理
    #
    # 改造前这里只判断"是不是寒暄"，其他问题一律走检索。
    # 实测发现这不够：
    #   · "明天天气怎么样" 走检索 → 命中沾边文档 → 模型编一个天气
    #   · "帮我预测股价"   走检索 → 给出预测（违反合规禁令，危险）
    #   · "红烧肉怎么做"   走检索 → 白跑一趟
    #
    # 现在分流成五类，每类有不同处理。
    _domain = get_domain()
    router_result = route(question, _domain)

    if router_result.intent != Intent.KNOWLEDGE:
        # 这四类都直接给固定回应，不检索、不调生成模型
        yield ("answer", {"text": router_result.reply})
        yield ("done", {
            "latency_ms": int((time.time() - started) * 1000),
            "citations": [],
            "has_answer": True,
            "intent": router_result.intent.value,
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
        # 熔断闸门：外部服务连续失败到阈值后，这里直接快速失败。
        #
        # ⚠️ 位置很关键：必须放在**所有模型调用之前**。
        #    之前放在问题改写之后，导致上游故障时改写仍会先打一次请求，
        #    熔断的保护打了折扣（改写、自检、重做都会穿透）。
        #
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

        # 0.9) 多轮问题改写
        #
        # 为什么要在检索之前做：
        #   用户在追问里常说「这三家」「它们」「上述公司」，这些指代词
        #   在知识库文档里一个字都没有。直接拿去检索必然空手而归，
        #   用户看到的就是"没有找到相关资料" —— 明明上一轮刚答过。
        #
        # 改写后（「这几家谁的指标最好」->「公司A 公司B 公司C 指标名 对比」）
        # 才能检索到正确的文档。
        #
        # 只在**有历史**时改写：单轮没有指代可消解，多一次调用纯属浪费。
        # 改写失败会自动回退原问题（见 query_rewrite 的实现），不会影响主流程。
        search_query = await rewrite_query(question, history)

        # 1) 检索知识库（retriever 内部已完成文档级去噪与截断）
        hits = await retrieve(search_query, kb_ids=kb_ids, top_k=settings.retrieval_top_k)
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


        # ---- 工具调用循环 ----
        #
        # ⚠️ 必须放在熔断检查**之后**。
        #   之前放在前面时，工具循环会绕过熔断直接调模型 ——
        #   熔断的意义就是"外部服务挂了就别再打它了"，
        #   绕过它等于熔断失效。这是测试发现的真实缺陷。
        #
        # 为什么在流式生成之前做这一轮：
        #   工具调用是"模型说要调什么 → 程序执行 → 结果塞回去 → 模型再答"，
        #   中间轮次没有内容给用户看，用非流式最简单可靠。
        #   只有最终那一轮才走流式输出。
        domain_tools = get_tools_for(_domain)
        if domain_tools and settings.tool_max_rounds > 0:
            messages = await _run_tool_loop(
                messages=messages,
                tools=domain_tools,
                llm=llm,
                max_rounds=settings.tool_max_rounds,
            )

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
        #
        # ⚠️ 这里有个"开头缓冲"机制，原因：
        #   部分模型会把内部独白写进 answer 通道，例如
        #       "用户问某某指标。资料显示某个数值。回答简洁，标注[1]。"
        #   然后才给正式答案。
        #
        #   流式已经发出去的文本**收不回来**，所以必须在开头先攒一会儿，
        #   确认这几句不是独白再放行。
        #
        #   缓冲多大：够装下一两句独白。攒到 _HEAD_BUFFER 就放行，
        #   避免用户等太久（正文长的时候不能一直憋着）。
        _HEAD_BUFFER = 200

        answer_parts: list[str] = []
        head_buffer = ""      # 尚未放行的开头文本
        head_released = False  # 开头是否已经放行
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

                if head_released:
                    # 开头已放行，后续正常转发
                    yield ("answer", {"text": content})
                else:
                    head_buffer += content
                    # 攒够了就处理一次：剥掉开头独白后整体放行
                    if len(head_buffer) >= _HEAD_BUFFER:
                        cleaned = strip_thinking_safe(head_buffer)
                        if cleaned:
                            yield ("answer", {"text": cleaned})
                        head_released = True

        # 流结束了但开头还没放行（回答很短）：在这里补发
        if not head_released and head_buffer:
            cleaned = strip_thinking_safe(head_buffer)
            if cleaned:
                yield ("answer", {"text": cleaned})
            head_released = True

        # 5) 答案自检（仅领域开启时）
        #
        # 为什么放在这里而不是流式过程中：
        #   自检需要**完整答案**才能判断。流式过程中答案还没成型，
        #   无法检查"关键数字有没有依据"。
        #
        # 为什么先把答案发出去再自检：
        #   已经流式发出的文本收不回来。开着自检时更好的做法是
        #   **不流式**（见下面的 _verify_and_fix 分支），
        #   但为了不破坏现有调用方的行为，这里做的是"事后自检 + 更正"：
        #   发现问题时追加一条更正消息，而不是假装没发生。
        full_answer = "".join(answer_parts)

        # 自检前再检查一次熔断：整轮问答可能耗时很久，
        # 期间上游可能已经跳闸。不检查的话会继续打请求，进一步压垮上游。
        if (
            _domain.answer_verify_rounds > 0
            and citations
            and full_answer.strip()
            and ai_guard.llm_breaker.allow()
        ):
            # ------------------------------------------------------------
            # 自检 + 打回重做循环
            #
            # 为什么要循环而不是固定做一次（这是一个真实缺陷的修复）：
            #   配置字段叫 answer_verify_rounds（复数，语义是"最多重试几次"），
            #   finance 领域配了 2。但原实现写死"只重做一次"，
            #   于是配 1 和配 2 行为完全一样 —— 配置形同虚设。
            #
            #   用户的原话是「自检没通过的话，希望能够打回，
            #   然后在此基础上进行完善，再继续」——
            #   "再继续"意味着允许多轮，不是一轮就放弃。
            #
            # 为什么要有上限：
            #   每轮重做都是一次完整的模型调用。没有上限时，
            #   如果自检始终不通过（比如资料本身矛盾），会一直烧钱。
            #
            # 循环结构：
            #   第 0 轮：检查初次答案
            #   第 N 轮（N < rounds）：不合格 → 带着原因重做 → 再检查
            #   用完次数仍不合格 → 明确告知，绝不硬编
            # ------------------------------------------------------------
            rounds = max(0, int(_domain.answer_verify_rounds))
            attempt = 0
            last_reason = ""
            verified_ok = False

            while attempt <= rounds:
                verdict = await verify_answer(
                    question, full_answer, citations, _domain, client=client
                )

                if verdict.ok:
                    if attempt == 0:
                        yield ("verified", {"ok": True})
                    else:
                        # 重做后通过了：把修正版整体替换掉已流出的旧答案
                        yield ("replace", {"text": full_answer})
                        yield ("verified", {"ok": True, "attempts": attempt})
                    verified_ok = True
                    break

                last_reason = verdict.reason

                # 还有重做次数吗？
                if attempt >= rounds:
                    break

                attempt += 1
                logger.info(
                    "自检未通过（第 %d/%d 轮），尝试重做：%s",
                    attempt, rounds, verdict.reason[:120],
                )

                fixed = await _regenerate_with_hint(
                    question=question,
                    citations=citations,
                    history=history,
                    hint=build_retry_hint(verdict.reason),
                    client=client,
                )

                if not fixed:
                    # 重做本身失败（模型超时/空响应）→ 不再重试，直接如实告知
                    logger.warning("重做未产出内容，停止重试")
                    break

                full_answer = fixed

            if not verified_ok:
                # 用完所有轮次仍不合格 → 明确告知，绝不硬编
                msg = (
                    "抱歉，我无法基于现有资料给出可靠的回答。"
                    "资料中的信息不足或存在不一致，建议补充资料后再试，"
                    "或咨询相关专业人士。"
                )
                yield ("replace", {"text": msg})
                full_answer = msg
                yield ("verified", {"ok": False, "reason": last_reason})

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
