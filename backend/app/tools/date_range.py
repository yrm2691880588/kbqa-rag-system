"""日期区间解析工具：把「去年 Q3 到今年 Q2」这类表达换成具体日期。

## 为什么需要它（不是画蛇添足）

模型对相对时间的理解**非常不可靠**。实测常见错误：

    今天 2026-11-16
    用户问「去年 Q3」
    模型可能理解成：2026 年 Q3（今年）或 2025 年 Q3（正确）

「今年 Q2」也可能被理解成 2025 Q2（错）或 2026 Q2（对）。

财经数据**差一年就全错**，不能靠模型猜。

## 为什么不用现成的库

可选方案：
  · `dateparser` 三方库 —— 支持自然语言，但要装依赖，且对"财务季度"的理解未必准
  · `dateutil.relativedelta` —— 只做相对时间减法，不理解"Q3"
  · 自己写 —— 财务语义很固定（年度/季度/半年），约 150 行足够

财务时间表达是很小的封闭集合，自己写更准、零依赖。

## 支持哪些说法

    相对：今年 / 去年 / 前年 / 明年
    季度：Q1 Q2 Q3 Q4，或 一季度 / 二季度……
    半年：上半年 / 下半年 / H1 / H2
    组合：去年Q3、今年Q2、2025年Q3、去年第三季度
    范围：去年Q3到今年Q2、2025年全年
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.tools.base import ToolResult


@dataclass(frozen=True)
class Period:
    """一个时间区间。"""

    label: str
    """人类可读的名称，如「2025 年 Q3」。"""

    start: date
    end: date
    """区间结束日（含）。"""

    def describe(self) -> str:
        return f"{self.label}（{self.start.isoformat()} ~ {self.end.isoformat()}）"


# 季度 -> (起始月, 结束月)
_QUARTER_MONTHS = {
    1: (1, 3),
    2: (4, 6),
    3: (7, 9),
    4: (10, 12),
}

# 半年 -> (起始月, 结束月)
_HALF_MONTHS = {
    1: (1, 6),
    2: (7, 12),
}


def _month_end(year: int, month: int) -> date:
    """某年某月的最后一天。"""
    if month == 12:
        return date(year, 12, 31)
    # 下个月 1 号减一天
    from datetime import timedelta

    return date(year, month + 1, 1) - timedelta(days=1)


def _quarter(year: int, q: int) -> Period:
    sm, em = _QUARTER_MONTHS[q]
    return Period(
        label=f"{year} 年 Q{q}",
        start=date(year, sm, 1),
        end=_month_end(year, em),
    )


def _half(year: int, h: int) -> Period:
    sm, em = _HALF_MONTHS[h]
    return Period(
        label=f"{year} 年 {'上' if h == 1 else '下'}半年",
        start=date(year, sm, 1),
        end=_month_end(year, em),
    )


def _year(year: int) -> Period:
    return Period(label=f"{year} 年", start=date(year, 1, 1), end=date(year, 12, 31))


# 中文数字 -> 阿拉伯数字
_CN_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4,
    "1": 1, "2": 2, "3": 3, "4": 4,
}


def _resolve_year(token: str, today: date) -> int | None:
    """把「今年/去年/前年/明年/2025」解析成具体年份。"""
    t = token.strip()
    if not t:
        return None

    if re.fullmatch(r"\d{4}", t):
        return int(t)

    mapping = {
        "今年": today.year,
        "本年": today.year,
        "去年": today.year - 1,
        "上年": today.year - 1,
        "前年": today.year - 2,
        "明年": today.year + 1,
        "次年": today.year + 1,
    }
    return mapping.get(t)


def parse_period(text: str, today: date | None = None) -> Period | None:
    """从一段文字里解析出一个时间区间。

    Args:
        text: 用户的问题原文或其中的时间片段。
        today: 参考日期（测试可注入）。

    Returns:
        Period 或 None（解析不出时）。
    """
    if today is None:
        today = date.today()

    s = (text or "").strip()
    if not s:
        return None

    # ---- 年份 + 季度 ----
    # 形式：去年Q3 / 今年第二季度 / 2025年Q3 / 2025Q3 / 2025年三季度
    m = re.search(
        r"(今年|去年|前年|明年|本年|上年|次年|\d{4})\s*年?\s*[Qq]\s*([1-4])",
        s,
    )
    if m:
        year = _resolve_year(m.group(1), today)
        if year:
            return _quarter(year, int(m.group(2)))

    m = re.search(
        r"(今年|去年|前年|明年|本年|上年|次年|\d{4})\s*年?\s*第?\s*([一二三四1-4])\s*季度",
        s,
    )
    if m:
        year = _resolve_year(m.group(1), today)
        q = _CN_NUM.get(m.group(2))
        if year and q:
            return _quarter(year, q)

    # ---- 年份 + 半年 ----
    m = re.search(r"(今年|去年|前年|明年|本年|上年|次年|\d{4})\s*年?\s*([Hh])\s*([12])", s)
    if m:
        year = _resolve_year(m.group(1), today)
        if year:
            return _half(year, int(m.group(3)))

    m = re.search(r"(今年|去年|前年|明年|本年|上年|次年|\d{4})\s*年?\s*(上|下)半年", s)
    if m:
        year = _resolve_year(m.group(1), today)
        if year:
            return _half(year, 1 if m.group(2) == "上" else 2)

    # ---- 单独一个季度（默认本年）----
    m = re.search(r"(?<!\d)[Qq]\s*([1-4])(?!\d)", s)
    if m and not re.search(r"(今年|去年|前年|明年|\d{4})", s):
        return _quarter(today.year, int(m.group(1)))

    # ---- 只有年份 ----
    m = re.search(r"(今年|去年|前年|明年|本年|上年|次年|\d{4})\s*年?", s)
    if m:
        year = _resolve_year(m.group(1), today)
        if year:
            return _year(year)

    return None


def parse_range(text: str, today: date | None = None) -> tuple[Period, Period] | None:
    """解析「A 到 B」形式的时间区间。

    为什么单独一个函数：
        用户问「去年Q3到今年Q2的增长率」时，需要的是**两个**区间
        （比较基准 + 当期），而不是一个。
        如果只返回一个，模型还得自己拆分，容易错。
    """
    if today is None:
        today = date.today()

    s = (text or "").strip()

    # 找分隔词，把句子切成两半
    m = re.search(r"(到|至|~|～|-|—)", s)
    if not m:
        return None

    left = s[: m.start()]
    right = s[m.end() :]

    p1 = parse_period(left, today)

    # 右侧可能省略了年份（「2025年Q1至Q4」），要沿用左侧的年份。
    # 不处理的话 Q4 会被解析成"本年的 Q4"，跨年就全错了。
    year_in_left = _period_year(p1) if p1 else None
    if year_in_left and not re.search(r"(今年|去年|前年|明年|本年|上年|次年|\d{4})", right):
        right_with_year = f"{year_in_left}年{right}"
        p2 = parse_period(right_with_year, today)
    else:
        p2 = parse_period(right, today)

    if p1 and p2:
        return (p1, p2)
    return None


def _period_year(period: Period | None) -> int | None:
    """从已解析的区间里取出年份（用于补全另一侧）。"""
    if period is None:
        return None
    return period.start.year


class DateRangeTool:
    """日期区间解析工具。"""

    name = "date_range"

    @property
    def description(self) -> str:
        """工具说明。

        ⚠️ 用 property 而不是类属性，因为要动态拼今天的日期。
        之前写成 `f"今天的日期是 {{today}}。"` —— 转义写错了，
        模型看到的字面就是 `{today}`，等于没告诉它今天几号。
        工具描述里带动态内容时，不要用类属性 + f-string 转义，
        直接用 property 最不容易出错。
        """
        today = date.today().isoformat()
        return (
            "把相对时间表达解析成具体日期。"
            "**当问题里出现「去年Q3」「今年Q2」「上个月」「最近三年」"
            "这类相对时间时，必须先调用本工具确认具体日期范围**，"
            "不要自己推算 —— 财务数据差一年就全错。"
            f"今天的日期是 {today}。"
        )

    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "含时间表达的文字，例如「去年Q3」「今年第二季度」"
                    "「去年Q3到今年Q2」"
                ),
            }
        },
        "required": ["text"],
    }

    async def run(self, **kwargs) -> ToolResult:
        text = str(kwargs.get("text", "")).strip()
        today = date.today()

        if not text:
            return ToolResult(text="解析失败：没有提供时间文字", ok=False)

        # 先尝试"区间"（A 到 B）
        rng = parse_range(text, today)
        if rng:
            p1, p2 = rng
            return ToolResult(
                text=(
                    f"「{text}」解析为两个时间区间：\n"
                    f"  起点区间：{p1.describe()}\n"
                    f"  终点区间：{p2.describe()}\n"
                    f"（今天是 {today.isoformat()}）"
                ),
                ok=True,
                meta={
                    "start": [p1.start.isoformat(), p1.end.isoformat()],
                    "end": [p2.start.isoformat(), p2.end.isoformat()],
                },
            )

        # 再尝试单个区间
        p = parse_period(text, today)
        if p:
            return ToolResult(
                text=(
                    f"「{text}」解析为：{p.describe()}\n"
                    f"（今天是 {today.isoformat()}）"
                ),
                ok=True,
                meta={"start": p.start.isoformat(), "end": p.end.isoformat()},
            )

        return ToolResult(
            text=(
                f"无法解析「{text}」中的时间表达。"
                f"支持的说法：今年/去年/前年、Q1~Q4、上半年/下半年、2025年Q3 等。"
                f"（今天是 {today.isoformat()}）"
            ),
            ok=False,
        )
