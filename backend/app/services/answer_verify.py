"""答案自检：生成后检查"这个回答靠不靠谱"，不合格就打回重做。

## 为什么需要（尤其在答错代价高的领域）

模型有一个根深蒂固的倾向：**即使资料里没有，它也会把答案编出来**。
表现是语气笃定、格式规范、数字具体 —— 用户完全看不出是编的。

在数据敏感的场景，编一个具体数字可能被当成决策依据；
在专业咨询场景，可能被当成专业建议。**代价不是难看，是危险。**

现有的防线只有一道：检索分数低于阈值就说"没找到"。
但检索一旦命中（哪怕只是沾边），模型就会开始编。

## 自检检查什么

**只检查能客观验证的东西**，不检查"内容对不对"：

1. 回答里的关键数字，能不能在资料里找到？
2. 回答是否明确超出了资料范围（资料没说，却说了）？
3. 是否答非所问？
4. 是否违反领域禁令？

**为什么不检查"内容对不对"**：
    那需要领域专家的知识。用另一个模型判断"这个财务分析对不对"
    本身就是不可靠的 —— 它和生成答案的模型一样会编。
    自检只能守"有没有依据"这条线，不能守"对不对"这条线。

## 打回机制

不合格时**必须把失败原因告诉模型**再重做。
只重试同一个提示词，大概率得到同样的错误答案。

    第 1 次：答案缺依据 → "你说的 142.7 亿在资料里找不到"
    第 2 次：模型修正 → 引用资料里真实的数字

## 用完了次数怎么办

**明确告诉用户"我无法给出可靠回答"，绝不硬编。**

这是答错代价高的领域的底线：宁可不答，不能瞎答。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.core.config import settings
from app.domain.base import DomainSpec

logger = logging.getLogger("kbqa.answer_verify")


@dataclass(frozen=True)
class VerifyResult:
    """自检结论。"""

    ok: bool
    """是否通过。"""

    reason: str = ""
    """不通过的原因（会喂给模型让它重做）。"""

    @staticmethod
    def passed() -> "VerifyResult":
        return VerifyResult(ok=True)

    @staticmethod
    def failed(reason: str) -> "VerifyResult":
        return VerifyResult(ok=False, reason=reason)


# 自检提示词。
#
# 设计要点：
#   · 只让它判断"有没有依据"，不判断"内容对不对"
#   · 要求输出 JSON，便于程序解析
#   · 明确的通过标准 —— 否则模型会过度挑剔，把好答案也判成不合格
_VERIFY_SYSTEM = (
    "你是一个答案核查器。你要判断一段回答是否**有资料依据**。\n"
    "\n"
    "只检查这四件事：\n"
    "1. 回答里的关键事实（数字、日期、名称、比例），能否在【参考资料】里找到依据？\n"
    "2. 回答是否说了资料里完全没有的内容（编造）？\n"
    "3. 回答是否答非所问（问的是 A，答的是 B）？\n"
    "4. 回答是否违反【禁令】？\n"
    "\n"
    "**不要**评价回答写得好不好、分析得深不深、结论对不对 —— "
    "那需要行业专家，不是你的职责。\n"
    "如果回答的内容都能在资料里找到依据，就判定通过。\n"
    "\n"
    "按此 JSON 格式输出，不要输出其他内容：\n"
    '{"ok": true/false, "reason": "不通过时的具体原因，通过时留空"}\n'
)


def _build_reference_text(citations: list[dict], limit: int = 4000) -> str:
    """把检索到的资料拼成自检用的参考文本。

    为什么限制长度：
        自检是"再问一次模型"，资料太长会显著增加成本与延迟。
        取前 4000 字符通常足够覆盖关键数字（检索结果已按相关度排序）。
    """
    parts = []
    total = 0
    for i, c in enumerate(citations, 1):
        text = (c.get("content") or c.get("text") or "").strip()
        if not text:
            continue
        piece = f"[{i}] {text}"
        if total + len(piece) > limit:
            piece = piece[: max(0, limit - total)]
        parts.append(piece)
        total += len(piece)
        if total >= limit:
            break
    return "\n\n".join(parts)


async def verify_answer(
    question: str,
    answer: str,
    citations: list[dict],
    domain: DomainSpec,
    client: AsyncOpenAI | None = None,
) -> VerifyResult:
    """检查回答是否有资料依据。

    Args:
        question: 用户问题。
        answer: 模型生成的完整回答。
        citations: 检索到的资料片段。
        domain: 领域配置（提供禁令）。
        client: 测试可注入 mock 客户端。

    Returns:
        VerifyResult。**任何异常都返回"通过"** ——
        自检挂掉不应该让用户拿不到答案（那等于用一个辅助功能把主功能搞瘫）。
    """
    # 没开自检：直接通过（调用方本应判断，这里兜一层）
    if domain.answer_verify_rounds <= 0:
        return VerifyResult.passed()

    # 空答案：没有可检查的内容
    if not (answer or "").strip():
        return VerifyResult.passed()

    # 没有资料却生成了答案 —— 这本身就是问题，直接判不通过
    if not citations:
        return VerifyResult.failed(
            "没有任何参考资料，但给出了回答。这种情况应该告知用户没有找到资料。"
        )

    try:
        llm = client
        if llm is None:
            from app.services.rag import _get_llm_client

            llm = _get_llm_client()

        ref = _build_reference_text(citations)
        forbidden_text = "；".join(domain.forbidden) if domain.forbidden else "（无特别禁令）"

        user_prompt = (
            f"【用户问题】\n{question}\n\n"
            f"【参考资料】\n{ref}\n\n"
            f"【禁令】\n{forbidden_text}\n\n"
            f"【待核查的回答】\n{answer[:3000]}\n\n"
            "请判断这个回答是否有资料依据，按 JSON 输出。"
        )

        resp = await llm.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": _VERIFY_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,   # 核查要确定性
            max_tokens=200,
            stream=False,
        )

        raw = ""
        if resp.choices:
            raw = resp.choices[0].message.content or ""

        # 模型有时会用 ```json 包裹
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # 解析不出 JSON：保守起见判"通过"。
            #
            # 为什么不判不通过：
            #   判不通过会触发重做，而重做又可能解析失败 —— 死循环。
            #   且解析失败说明是**我们的解析**有问题，不是答案有问题，
            #   不应该因此让用户拿不到答案。
            logger.warning("自检输出无法解析为 JSON，按通过处理：%r", raw[:200])
            return VerifyResult.passed()

        ok = bool(data.get("ok", True))
        reason = str(data.get("reason") or "").strip()

        if ok:
            return VerifyResult.passed()

        logger.info("自检未通过：%s", reason[:150])
        return VerifyResult.failed(reason or "回答缺少资料依据")

    except Exception as exc:  # noqa: BLE001
        # 自检失败不能影响主流程。记 warning 不记 error ——
        # 它是可选增强，不是系统故障。
        logger.warning("自检执行失败，按通过处理：%s", exc)
        return VerifyResult.passed()


def build_retry_hint(reason: str) -> str:
    """把自检的失败原因转成给模型的修正指令。

    为什么单独成函数：
        这段话的措辞直接影响重做的效果。集中在一处便于调整。
        实测发现"请重做"这种笼统指令没用，必须说清"哪里不对"。

    ⚠️ 换行必须用真正的换行符，不能写字面量 `
`：
        这里最初写成 "\n\n【重要...】"，Python 里是**两个字符**（反斜杠 + n），
        模型收到的是一整行挤在一起的符号，指令的可读性和效果都大打折扣。
        实测确认过：'是否含真换行: False / 是否含字面反斜杠n: True'。

        改用 chr(10) 拼接最稳 —— 本项目的转义多次被工具链吞掉，
        在源码里直接写 
 有风险。
    """
    LF = chr(10)
    lines = [
        "",
        "",
        "【重要：上一版回答有这些问题，请修正后重新回答】",
        f"- {reason}",
        "要求：",
        "1. 只使用参考资料里出现过的信息，不要补充资料外的内容",
        "2. 如果资料里确实没有这个信息，直接说明「资料中未提及」，不要推测",
        "3. 关键数字必须与资料一致",
        "",
    ]
    return LF.join(lines)

