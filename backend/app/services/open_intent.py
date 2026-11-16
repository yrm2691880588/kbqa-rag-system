"""开放性意图检测（"列举/总览"类问题识别）。

背景
----
当用户问"介绍一下本商品信息""有哪些商品可选"这类开放式问题时,
向量检索往往找不到强命中（库内多为具体商品的说明书/参数表,缺少总览文档），
RAG 流程会走到 no_hit 给出冷冰冰的"未找到资料"——体验不友好。
为此,RAG 主流程会在 no_hit 之前先调用本模块:命中开放性意图时,
主动列出知识库里有哪些商品可问,而不是简单拒绝。

用法
----
>>> detect_open_intent("你好，请介绍一下本商品信息")
True
>>> detect_open_intent("S10 的电池容量是多少？")
False
>>> detect_open_intent("请问手机有什么快充功能？")
False  # 虽含"有什么",但带具体属性词+型号,按具体问题交给 RAG

注意
----
- **避免误伤具体问题**：含具体商品关键词（型号/品类）时,即使命中意图词,也按具体问题处理。
- **避免误伤小问题**：空串/纯标点/纯空白应返回 False,不应触发"列商品"。
"""

import re


# ---------------------------------------------------------------------------
# 词表来源：领域模块
# ---------------------------------------------------------------------------
# 这两组词原本写死在本文件里（「商品」「手机」「电池」……），
# 现在由领域模块提供 —— 因为不同领域的用词完全不同：
#   电商：有哪些商品 / S10 / 手机
#   财经：有哪些股票 / 招商银行 / 市盈率
#
# 判断**逻辑**仍留在这里（它是通用的：先命列举词、再排除具体词），
# 只有词表内容移到了 app/domain/<name>/domain.py。
#
# 为什么用函数而不是模块级常量：
#   模块级常量在 import 时就固定，测试里想切换领域就必须重新 import 模块，
#   很别扭。改成函数调用后，配合 app.domain.reset_for_testing() 就能切换。


def _open_intent_keywords() -> tuple[str, ...]:
    """当前领域的列举意图词。"""
    from app.domain import get_domain

    return get_domain().open_intent_keywords


def _specific_hints() -> tuple[str, ...]:
    """当前领域的具体对象词（命中即不算列举）。"""
    from app.domain import get_domain

    return get_domain().specific_hints


def _foreign_hints() -> tuple[str, ...]:
    """别的领域的典型词（出现在本领域说明用户多半问错了库）。"""
    from app.domain import get_domain

    return get_domain().foreign_hints



def _normalize(question: str) -> str:
    """归一化：小写 + 去前后空白。"""
    if not question:
        return ""
    return question.strip().lower()


def detect_open_intent(question: str) -> bool:
    """判断用户是否在问"列举/总览"类开放性问题。

    触发条件：**同时满足**
    1. 句子非空
    2. 包含当前领域配置的列举意图关键词
    3. 不含当前领域配置的具体对象提示词

    Args:
        question: 用户输入原文。

    Returns:
        True=开放性意图，应主动列出清单引导；False=具体问题，走 RAG。
    """
    q = _normalize(question)
    if not q:
        return False

    # 必须先命中"列举意图"
    if not any(kw in q for kw in _open_intent_keywords()):
        return False

    # 若带具体型号/品类/属性词，按具体问题处理
    if any(hint.lower() in q for hint in _specific_hints()):
        return False

    # 若带**别的领域**的典型词，也按具体问题处理。
    # 例：财经领域里问「介绍一下本商品」—— 词表没有"本商品"，
    # 但 foreign_hints 里有"商品"，据此拦住，避免列出一份股票清单。
    if any(hint.lower() in q for hint in _foreign_hints()):
        return False

    return True


def explain(question: str) -> dict:
    """诊断用途：返回详细判定信息，便于调试与单元测试。

    Returns:
        {
          "is_open": bool,
          "matched_keyword": 命中的关键词（可空）,
          "blocked_by_hint": 命中的具体提示词（可空）,
        }
    """
    q = _normalize(question)
    matched_kw = next((kw for kw in _open_intent_keywords() if kw in q), "")
    blocked_by = next((h for h in _specific_hints() if h.lower() in q), "")
    blocked_by_foreign = next((h for h in _foreign_hints() if h.lower() in q), "")
    return {
        "is_open": bool(q) and bool(matched_kw) and not blocked_by and not blocked_by_foreign,
        "matched_keyword": matched_kw,
        "blocked_by_hint": blocked_by,
        "blocked_by_foreign": blocked_by_foreign,
    }
