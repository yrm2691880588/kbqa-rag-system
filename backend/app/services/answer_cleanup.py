"""剥离回答开头的"内部思考"片段。

## 为什么需要（这不是提示词能完全解决的）

系统提示里已经要求"直接给答案，不要输出分析过程"（规则 7）。
但实测发现部分模型仍会在开头写：

    用户问海联银行2025年净利润。资料显示归属于母公司股东的净利润142.7亿元，
    同比增长6.1%。注意区分"净利润"在表中为142.7亿元。回答简洁，标注[1]。
    海联银行 2025 年归属于母公司股东的净利润为 **142.7 亿元**……

第一句到第三句都是模型的自言自语，真正的答案从第四句才开始。
用户看到的第一屏全是独白。

## 为什么用启发式而不是更复杂的方法

可选方案：
  · 让模型自己标记（不可靠，它经常忘）
  · 用另一个模型判断哪些是思考（多一次调用，且可能误判）
  · 启发式规则（本方案）

启发式在这里够用，因为这类独白有非常固定的特征：
  · 以"用户问"、"资料显示"、"回答"、"根据资料"开头
  · 常含"应"、"注意"、"简洁"这类指导自己的词
  · 后面紧跟一个空行或明显的正文起点

## 保守原则

**宁可保留，不可错删。**
如果拿不准，就不动 —— 删掉用户真正想看的内容，比保留一句多余的话更糟。

所以规则设计成"只有非常明确像独白时才删"，且最多只删开头的连续几行。
"""

from __future__ import annotations

import re


# 明确像"模型在跟自己说话"的开头模式。
# 只匹配**句首**，不匹配句子中间 —— 中间出现"用户问"可能是正常引用。
_THINKING_STARTERS = (
    "用户问",
    "用户询问",
    "用户想知道",
    "问题问的是",
    "资料显示",
    "根据资料",
    "根据检索",
    "检索结果",
    "回答应",
    "答案应",
    "应该回答",
    "注意区分",
    "需要注意",
    "用简体中文",
    "标注",
    "回答简洁",
    "组织答案",
)

# 单行里出现这些词，且整行不长（说明是指导性独白而非正文）
_GUIDANCE_WORDS = ("回答", "标注", "简洁", "分行", "表格", "角标", "引用")

# 一段"思考"最多能有多长（字符）。
# 太长的段落更可能是正文，不该删。
_MAX_THINKING_LINE_LEN = 160


def _is_thinking_line(line: str) -> bool:
    """判断单行是否是模型的内部独白。"""
    s = line.strip()
    if not s:
        return False

    # 太长的不当独白处理
    if len(s) > _MAX_THINKING_LINE_LEN:
        return False

    # 以明确的自言自语词开头
    if s.startswith(_THINKING_STARTERS):
        return True

    # 短行 + 指导性词汇 + 没有问号（问号说明是在跟用户说话）
    if len(s) <= 80 and "？" not in s and "?" not in s:
        if any(w in s for w in _GUIDANCE_WORDS) and s.startswith(("应", "要", "需要", "先", "然后")):
            return True

    return False


def strip_leading_thinking(text: str) -> str:
    """把回答开头的内部独白剥掉。

    Args:
        text: 模型输出的完整回答。

    Returns:
        剥掉开头独白后的文本。**如果剥离后没剩什么内容，返回原文** ——
        说明判断错了，宁可保留。

    只处理开头连续的独白行，不碰正文中间的内容：
    正文里出现"根据资料显示……"是正常的表述，删了就破坏可读性。
    """
    if not text:
        return text

    lines = text.split("\n")

    # 找到第一个"像正文"的行
    first_body = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if _is_thinking_line(s):
            continue
        first_body = i
        break

    # 没有独白，或全是独白 → 原样返回
    if first_body is None or first_body == 0:
        return text

    # 剥掉开头的独白后，剩下的内容要足够多才认为是"剥离成功"
    remaining = "\n".join(lines[first_body:]).strip()
    if len(remaining) < 20:
        # 剩太少了，说明前面那些可能就是正文，不该删
        return text

    return remaining


def strip_thinking_safe(text: str) -> str:
    """对外入口：包一层异常保护。

    剥离是"锦上添花"的能力，任何异常都不应影响回答展示。
    """
    try:
        return strip_leading_thinking(text)
    except Exception:  # noqa: BLE001
        return text
