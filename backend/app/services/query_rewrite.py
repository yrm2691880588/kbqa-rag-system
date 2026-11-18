"""多轮对话的问题改写（Query Rewriting）。

## 这个模块解决什么问题

用户在同一个会话里连续提问时，后一句经常带指代：

    第 1 轮：某公司、某公司和另一家公司，这三家有什么差异？
    第 2 轮：这几家谁的某项指标最好？

第 2 轮的「这三家」在知识库文档里**一个字都没有** ——
文档写的是具体公司名。向量检索拿「这三家」去搜，必然空手而归，
用户看到的就是"没有找到相关资料"。

## 怎么解决

检索**之前**，先用模型把问题改写成一句自包含、带具体实体的检索用问题：

    「这几家谁的某项指标最好？」
        ↓ 改写
    「公司A 公司B 公司C 指标名 对比」

改写后的句子去检索，就能命中三份资料。

## 设计约束（每条都有理由）

1. **只在有历史时改写**
   单轮对话没有指代可消解，改写纯属浪费一次模型调用。

2. **失败必须回退原文**
   改写是"增强"，不是必经路径。模型超时、返回空、格式不对 ——
   任何一种情况都退回原问题，让主流程照常跑。
   **绝不能因为改写失败导致用户问不了问题。**

3. **禁止编造实体**
   改写提示里明确要求"只能用对话历史中出现过的实体"。
   否则模型可能凭空造出一个公司名，检索返回完全无关的内容，
   比"没找到"更糟。

4. **长度上限**
   改写结果超过设定长度就直接丢弃用原文 ——
   说明模型没按要求输出一句话，而是在自由发挥。

5. **关掉就完全恢复原行为**
   配置开关关闭时不发任何额外请求，行为与改造前完全一致。
"""

from __future__ import annotations

import logging
import re

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger("kbqa.query_rewrite")

# 改写提示词。
#
# 为什么写得这么啰嗦：
#   实测发现模型很容易"顺手把问题也答了"或者"输出一段解释"。
#   必须用明确的句式约束它只做改写这一件事。
_REWRITE_SYSTEM = (
    "你是一个检索查询改写器。你的唯一任务是把用户的追问改写成一句"
    "可以独立检索的查询语句。\n"
    "规则：\n"
    "1. 只输出改写后的那一句话，不要解释、不要回答问题、不要加引号或前缀。\n"
    "2. 必须把指代词（如「这三家」「它们」「上述公司」「这个指标」）"
    "替换成对话历史里出现过的具体名称。\n"
    "3. **绝不能编造**对话历史里没有出现过的名称。如果无法确定指代对象，"
    "就保留原问题不变。\n"
    "4. 保留原问题里的关键限定（对比维度、时间范围、指标名）。\n"
    "5. 用空格分隔关键词，不要写成疑问句。\n"
    "6. 长度不超过 60 个字。"
)


def _clean_rewritten(text: str) -> str:
    """清理模型输出，去掉常见的多余包装。

    模型有时会输出：
        "改写后的查询：某公司 某公司 ..."
        "「某公司 某公司 ...」"
        "```\n某公司...\n```"
    这些包装如果不去掉，会直接影响检索效果（多了无关字词）。
    """
    t = (text or "").strip()
    if not t:
        return ""

    # 去掉代码块围栏
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)

    # 去掉「改写后的查询：」这类前缀
    t = re.sub(r"^(改写后?的?(查询|问题|语句)?[:：]\s*)", "", t)

    # 去掉包裹的引号（中文/英文/直角）
    t = t.strip().strip('"\'`「」『』【】')

    # 只取第一行（模型偶尔会多写一行解释）
    t = t.split("\n")[0].strip()

    return t


def _looks_reasonable(rewritten: str, original: str) -> bool:
    """判断改写结果是否可用。

    宁可不用改写，也不要用一个坏结果 ——
    坏的改写会把检索带到完全无关的方向，比"没找到"更误导用户。
    """
    if not rewritten:
        return False

    # 太长：说明模型没按要求输出一句话
    if len(rewritten) > settings.query_rewrite_max_len:
        return False

    # 太短：可能是空响应或单个标点
    if len(rewritten) < 2:
        return False

    # 一字未改：没有必要替换（省一点后续处理）
    if rewritten == original.strip():
        return False

    # 仍然是疑问句：说明没改写成功
    if rewritten.endswith(("?", "？")):
        # 但如果是"保留原问题"的情况，这条会被 _looks_reasonable 判否，
        # 调用方自然回退到原问题，符合预期。
        return False

    return True


async def rewrite_query(
    question: str,
    history: list[dict],
    client: AsyncOpenAI | None = None,
) -> str:
    """把带指代的追问改写成可独立检索的查询。

    Args:
        question: 用户当前问题。
        history: 对话历史 [{"role": "user"|"assistant", "content": "..."}]。
        client: 测试可注入 mock 客户端。

    Returns:
        改写后的查询；**无法改写时返回原问题**（调用方不需要处理 None）。

    Note:
        这个函数**不抛异常**。任何失败都回退到原问题 ——
        改写只是锦上添花，不能让用户的提问因此失败。
    """
    # 开关关闭 / 没有历史 → 直接用原问题，一次请求都不发
    if not settings.query_rewrite_enabled:
        return question
    if not history:
        return question
    if not (question or "").strip():
        return question

    try:
        llm = client
        if llm is None:
            # 延迟导入避免循环依赖（rag.py 里也有同样的客户端）
            from app.services.rag import _get_llm_client

            llm = _get_llm_client()

        # 只取最近几轮：太长的历史既费 token 又容易把改写带偏
        recent = history[-settings.query_rewrite_history_rounds * 2 :]
        convo = "\n".join(
            f"{'用户' if m.get('role') == 'user' else '助手'}：{(m.get('content') or '')[:300]}"
            for m in recent
        )

        user_prompt = (
            f"【对话历史】\n{convo}\n\n"
            f"【用户当前追问】\n{question}\n\n"
            "请输出改写后的检索查询（只输出一句话）。"
        )

        resp = await llm.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": _REWRITE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,      # 改写要确定性，不要发挥
            max_tokens=100,     # 一句话足够；给多了模型容易写废话
            stream=False,       # 只要一句话，不需要流式
        )

        raw = ""
        if resp.choices:
            raw = resp.choices[0].message.content or ""

        rewritten = _clean_rewritten(raw)

        if _looks_reasonable(rewritten, question):
            logger.info("问题改写：%r -> %r", question, rewritten)
            return rewritten

        logger.debug("改写结果不可用，回退原文：%r -> %r", question, rewritten)
        return question

    except Exception as exc:  # noqa: BLE001
        # 改写失败绝不能影响主流程。
        # 这里用 warning 而不是 error：它是可降级的能力，不是故障。
        logger.warning("问题改写失败，回退原文：%s", exc)
        return question
