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


# 列举/总览意图关键词（按从强到弱排列）
# 仅收录"想要一个清单/总览"的明确表达，不收录太宽的"有什么""怎么样"等。
_OPEN_INTENT_KEYWORDS: tuple[str, ...] = (
    "介绍一下本商品", "介绍一下商品信息", "介绍一下商品",
    "介绍下商品", "介绍下本商品",
    "介绍一下产品", "介绍下产品",
    "介绍一下",                       # 仅在无具体型号时算开放
    "有哪些商品", "有什么商品", "有什么可选", "有什么选择",
    "全部商品", "所有商品", "商品列表", "商品总览", "商品有哪些",
    "都有哪些商品", "都有什么商品",
    "你能介绍", "给我介绍", "给我说说",
    "你们有什么", "你们有哪些", "你们卖什么", "你们卖哪些",
)

# 命中"列举意图"后还必须**不包含**具体型号/品类词,才判为真正的开放性问题。
# 否则"介绍一下 S10"会变成"列商品",而 S10 是具体商品应走 RAG。
_SPECIFIC_HINTS: tuple[str, ...] = (
    # 型号类：字母+数字或常见手机/耳机型号
    "S10", "S20", "S30", "A8", "A10", "A20",
    "Pro", "Pro2", "Max", "Plus", "Mini", "Ultra", "Lite", "Neo", "Note",
    "Watch2", "Watch3", "Watch ", "Buds", "Free",
    # 商品品类（出现即代表用户说的是具体一类商品）
    "手机", "耳机", "手表", "净化器", "冰箱", "笔记本", "平板", "音箱", "电视", "相机",
    # 常见属性词（电池/快充/防水/屏幕 等 → 表明用户问的是具体属性,非列举）
    "电池", "续航", "充电", "快充", "屏幕", "摄像", "拍照", "像素",
    "防水", "降噪", "价格", "多少钱", "参数", "配置", "规格", "保修", "售后",
)


def _normalize(question: str) -> str:
    """归一化：小写 + 去前后空白。"""
    if not question:
        return ""
    return question.strip().lower()


def detect_open_intent(question: str) -> bool:
    """判断用户是否在问"列举/总览"类开放性问题。

    触发条件：**同时满足**
    1. 句子非空
    2. 包含列举意图关键词（_OPEN_INTENT_KEYWORDS）
    3. 不含具体型号/品类/属性提示词（_SPECIFIC_HINTS）

    Args:
        question: 用户输入原文。

    Returns:
        True=开放性意图，应主动列商品引导；False=具体问题，走 RAG。
    """
    q = _normalize(question)
    if not q:
        return False

    # 必须先命中"列举意图"
    if not any(kw in q for kw in _OPEN_INTENT_KEYWORDS):
        return False

    # 若带具体型号/品类/属性词，按具体问题处理
    if any(hint.lower() in q for hint in _SPECIFIC_HINTS):
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
    matched_kw = next((kw for kw in _OPEN_INTENT_KEYWORDS if kw in q), "")
    blocked_by = next((h for h in _SPECIFIC_HINTS if h.lower() in q), "")
    return {
        "is_open": bool(q) and bool(matched_kw) and not blocked_by,
        "matched_keyword": matched_kw,
        "blocked_by_hint": blocked_by,
    }
