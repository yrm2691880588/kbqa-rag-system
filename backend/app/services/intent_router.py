"""意图分流：判断一个问题该走哪条路。

## 为什么需要

现在系统对所有问题都走同一条路：检索 → 生成。
但不同问题的正确处理方式完全不同：

    用户：你好                    → 不该去查知识库，直接回应
    用户：明天天气怎么样          → 知识库里没有，模型也不知道，
                                   硬答就是编造
    用户：帮我预测下茅台股价      → 违反领域禁令，必须拒答
    用户：海联银行净利润多少      → 正常查库

全都走"检索 → 生成"的后果：
  · "你好"浪费一次检索
  · "明天天气"变成编造
  · "预测股价"可能真的给出预测（危险）

## 设计原则

1. **规则优先，模型兜底**
   常见情况（寒暄、明确的外部数据请求、明确的违规请求）
   用规则就能判断，便宜又快。只有拿不准的才调模型。

2. **拿不准就往"正常问答"走**
   宁可多检索一次，也不要把正常问题误判成超范围 ——
   误拒用户是比多花一次检索严重得多的错误。

3. **每个领域边界不同**
   财经领域拒答"天气"，但人脉知识库可能要靠"联系时机"给建议。
   所以判断依据来自领域配置（scope_description），不是写死的规则。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from app.core.config import settings
from app.domain.base import DomainSpec
from app.services.content_filter import check as content_check
from app.services.smalltalk import detect_smalltalk

logger = logging.getLogger("kbqa.intent_router")


class Intent(str, Enum):
    """问题的处理方式。"""

    SMALLTALK = "smalltalk"        # 闲聊：直接回应，不查库
    BLOCKED = "blocked"            # 违规：拒答
    NEED_EXTERNAL = "need_external"  # 需要实时数据：告知无法获取
    KNOWLEDGE = "knowledge"        # 知识库问答：走 RAG
    OUT_OF_SCOPE = "out_of_scope"  # 超出领域范围：拒答


@dataclass(frozen=True)
class RouteResult:
    """分流结论。"""

    intent: Intent
    # 直接可以返回给用户的话（SMALLTALK / BLOCKED / NEED_EXTERNAL / OUT_OF_SCOPE 用）
    reply: str = ""
    # 命中的原因，用于日志与调试
    reason: str = ""


# ---------------------------------------------------------------------------
# 规则：需要实时数据的问题
# ---------------------------------------------------------------------------
#
# 这些问题的共同点是：**答案不在任何静态知识库里**。
# 知识库是快照，天气/汇率/股价/新闻每秒都在变。
#
# 为什么必须拦住而不是让它答：
#   模型不知道明天的天气，但它不会说"我不知道"，而是会**编一个**。
#   在财经、医疗这类场景，编造是不能接受的。
_REALTIME_KEYWORDS = (
    # 天气
    "天气", "气温", "下雨", "降雨", "下雪", "台风", "雾霾", "空气质量",
    "明天冷", "明天热", "穿什么衣服",
    # 依赖天气的活动（用户不会说"天气"，但问题本质是要天气才能答）
    #   实测漏判：问「明天适合钓鱼吗」时判成了知识库问答，
    #   但它需要知道明天的天气才能回答。
    "适合钓鱼", "适合露营", "适合爬山", "适合出行", "适合郊游",
    "适不适合", "能去吗", "要不要带伞",
    # 行情
    "股价", "股价是多少", "实时行情", "现在多少钱", "大盘",
    "汇率", "美元汇率", "今天汇率",
    # 其他实时信息
    "新闻", "最新消息", "今天发生", "刚刚发生", "实时",
)


def _is_realtime_question(question: str) -> bool:
    """判断问题是否在问实时数据。

    只做关键词匹配 + 简单语境判断，不做复杂语义分析 ——
    误判的代价是"多拒答一次"，可以通过让用户换个问法来补救；
    而漏判的代价是"编造一个假答案"，后果严重得多。
    """
    q = question.strip()
    if not q:
        return False
    return any(kw in q for kw in _REALTIME_KEYWORDS)


# ---------------------------------------------------------------------------
# 规则：超出领域范围的问题
# ---------------------------------------------------------------------------
#
# 注意：这里**不做**业务内容判断（那需要模型），只拦明显无关的：
# 例如财经知识库里问"红烧肉怎么做"。这类判断靠领域配置的
# scope_description 让模型来做（见 _model_judge_out_of_scope）。


# ---------------------------------------------------------------------------
# 规则：领域禁令
# ---------------------------------------------------------------------------
#
# 领域的 forbidden 是自然语言描述（"不要预测涨跌"），不是关键词表。
# 直接匹配这些句子匹配不到用户的问题，所以这里维护一组
# **与禁令对应的用户问法关键词**。
#
# 为什么不用模型判断：
#   禁令是明确的合规红线，误判代价不对称 ——
#   漏判会给出违规回答（法律风险），误判只是多说一句"不能答"。
#   规则快且可解释，适合这种场景。
_FORBIDDEN_PATTERNS = (
    # 预测涨跌
    # 「预测」后面跟的对象很多：涨/跌/走势/行情/股价/价格/目标价
    # 少列一个就会漏判（实测「预测茅台股价」就漏了）
    ("预测", ("涨", "跌", "走势", "行情", "股价", "价格", "目标价", "趋势")),
    ("会涨吗", ()),
    ("会跌吗", ()),
    ("能涨到", ()),
    ("会不会涨", ()),
    ("会不会跌", ()),
    # 买卖建议
    ("该买", ()),
    ("该卖", ()),
    ("买入", ()),
    ("卖出", ()),
    ("抄底", ()),
    ("加仓", ()),
    ("减仓", ()),
    ("值得投资", ()),
    ("值得买", ()),
    ("推荐买", ()),
    ("推荐股", ()),
    # 「推荐一只股票」「推荐个标的」这类说法
    #   实测漏判：只写"推荐股"匹配不到"推荐一只股票"
    ("推荐", ("股", "标的", "基金", "债券")),
    ("买哪", ()),
    ("买什么股", ()),
    # 收益承诺
    ("保本", ()),
    ("收益多少", ()),
    ("能赚", ()),
    ("稳赚", ()),
)


def _match_forbidden(question: str, domain: DomainSpec) -> str:
    """判断问题是否触碰领域禁令。

    Returns:
        命中的禁令说明（用于提示用户），未命中返回空字符串。
    """
    # 领域没配禁令 → 不做这个判断
    if not domain.forbidden:
        return ""

    q = question.strip()
    for primary, followups in _FORBIDDEN_PATTERNS:
        if primary in q:
            # 有后续词时要求同时出现（如"预测" + "涨/跌"）
            if followups and not any(f in q for f in followups):
                continue
            # 找到对应的禁令原文，让用户知道为什么不能答
            for f in domain.forbidden:
                if any(kw in f for kw in ("涨", "跌", "预测", "买卖", "收益", "投资")):
                    return f
            return "该问题涉及投资建议类内容"
    return ""


def route(
    question: str,
    domain: DomainSpec,
    *,
    allow_model_judge: bool = False,
) -> RouteResult:
    """判断一个问题该怎么处理。

    Args:
        question: 用户问题。
        domain: 当前领域配置（提供边界与话术）。
        allow_model_judge: 是否允许用模型做兜底判断。
            **默认 False** —— 意味着本函数是纯规则、无副作用、可单测。
            调用方在需要更准判断时再打开（会多一次模型调用）。

    Returns:
        RouteResult，指明意图与可直接返回的文本。

    Note:
        这个函数**绝不抛异常**：分流失败时返回 KNOWLEDGE（正常问答），
        让主流程照常跑。分流是优化，不是必需品。
    """
    try:
        q = (question or "").strip()
        if not q:
            return RouteResult(Intent.KNOWLEDGE, reason="空问题，走默认")

        # ---- 1) 闲聊：复用现有的寒暄识别（已覆盖问候/致谢/自我介绍） ----
        greeting = detect_smalltalk(q)
        if greeting:
            return RouteResult(Intent.SMALLTALK, reply=greeting, reason="命中寒暄规则")

        # ---- 2) 违规：复用现有的敏感词过滤 ----
        if content_check(q, mask=False).hit:
            return RouteResult(
                Intent.BLOCKED,
                reply="抱歉，这个问题我无法回答。请提出与知识库内容相关的问题。",
                reason="命中内容过滤",
            )

        # ---- 2.5) 是否触碰领域禁令 ----
        #
        # 领域配置里的 forbidden 是明确的合规红线，例如财经领域的
        # 「不预测涨跌」「不推荐买卖」。这类问题不该走实时数据分支
        # （那条会说"我无法获取实时数据"，听起来像能力不足），
        # 而应该明确说明"这类问题不在我的回答范围内"。
        #
        # 判断方式：禁令里通常包含明确的问题类型词。
        # 这里做关键词匹配，宁可漏判（让它走正常检索，
        # 由提示词里的禁令兜底）也不误伤正常问题。
        blocked_hit = _match_forbidden(q, domain)
        if blocked_hit:
            return RouteResult(
                Intent.BLOCKED,
                reply=(
                    "这个问题涉及合规限制，我无法回答。"
                    + (
                        (chr(10) + chr(10) + f"具体限制：{blocked_hit}")
                        if blocked_hit else ""
                    )
                ),
                reason=f"触碰领域禁令：{blocked_hit}",
            )

        # ---- 3) 需要实时数据 ----
        #
        # 这一步要在"查库"之前判断：实时问题的答案**不可能**在知识库里，
        # 让检索白跑一趟还会因为"命中了含相似词的文档"而给出误导答案。
        if _is_realtime_question(q):
            hint = domain.external_fact_hint or "我无法获取实时信息。"
            # 给建议而不是一句话打死：
            # 用户问天气往往是"想做事"，告诉他我能提供什么才有用。
            reply = (
                f"{hint}\n\n"
                "不过我可以在知识库里帮你查相关的记录或资料。"
                "如果你有需要，可以把问题换成「知识库里关于 X 的记录」。"
            )
            return RouteResult(Intent.NEED_EXTERNAL, reply=reply, reason="命中实时数据关键词")

        # ---- 4) 交给调用方决定是否用模型判断范围 ----
        # 规则判断不了"这个问题在不在这本知识库的范围内" ——
        # 那需要理解知识库里有什么。这里留出接口。
        if allow_model_judge and domain.scope_description:
            # 模型判断放在调用方实现（异步），本函数保持同步可单测
            return RouteResult(Intent.KNOWLEDGE, reason="待模型判断范围")

        # ---- 5) 默认：正常知识库问答 ----
        return RouteResult(Intent.KNOWLEDGE, reason="默认走知识库问答")

    except Exception as exc:  # noqa: BLE001
        # 分流出错绝不能挡住问答本身
        logger.warning("意图分流异常，回退到知识库问答：%s", exc)
        return RouteResult(Intent.KNOWLEDGE, reason="分流异常，回退默认")
