"""检索质量评估指标：把"检索准不准"从感觉变成数字。

为什么必须有个数字（大白话）：
    调检索参数（召回条数、相似度阈值、要不要开重排）时，如果没有一把尺子，
    就只能问"我感觉好像好了一点？"—— 这种判断靠不住，而且一旦改了别的地方，
    也没法知道是变好了还是变坏了。

    有了评估集（一批"问题 + 正确答案应该来自哪篇资料"），每次改完跑一遍，
    就能直白地说："重排让命中率从 72% 提到 86%"，而不是"我感觉好了"。

本文件只做**算术**（纯函数，好测）；真正去跑检索的部分在 scripts/eval_retrieval.py。

两个指标（都用最通俗的话解释）：
  · 命中率（hit@k）：在前 k 条结果里，有没有找到那片"应该找到"的资料。
    衡量"找得到吗"。取 k=1 时就是最严格的"第一条就对了吗"。
  · 平均倒数排名（MRR）：第一条就命中得 1 分，第二条才命中得 0.5 分，
    第三条得 0.33 分……都没命中得 0 分。平均起来，
    衡量"找得有多靠前"—— 答案排在第 1 位和第 5 位，对用户体验是完全不同的。
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _normalize(names) -> set[str]:
    """统一成小写、去首尾空格的集合，避免"文件名大小写不同"被判成没命中。"""
    return {str(n).strip().lower() for n in (names or []) if str(n).strip()}


def hit_at_k(ranked_docs: list[str], expect_docs, k: int = 1) -> bool:
    """前 k 条里有没有命中任意一个"期望资料"。"""
    if k <= 0:
        return False
    top = _normalize(ranked_docs[:k])
    return bool(top & _normalize(expect_docs))


def reciprocal_rank(ranked_docs: list[str], expect_docs) -> float:
    """第一条命中的位置取倒数；一条都没命中返回 0。

    例：期望资料排在第 1 位 → 1.0；第 2 位 → 0.5；第 4 位 → 0.25；没出现 → 0.0。
    """
    expect = _normalize(expect_docs)
    if not expect:
        return 0.0
    for i, doc in enumerate(ranked_docs, start=1):
        if str(doc).strip().lower() in expect:
            return 1.0 / i
    return 0.0


@dataclass
class CaseResult:
    """一个问题的评估结果。"""

    question: str
    ranked_docs: list[str] = field(default_factory=list)
    expect_docs: list[str] = field(default_factory=list)
    hit_at_1: bool = False
    hit_at_3: bool = False
    hit_at_5: bool = False
    rr: float = 0.0
    error: str = ""

    @property
    def matched_rank(self) -> int | None:
        """期望资料第一次出现在第几位（没命中则 None）—— 排查时最有用的那个数。"""
        expect = _normalize(self.expect_docs)
        for i, doc in enumerate(self.ranked_docs, start=1):
            if str(doc).strip().lower() in expect:
                return i
        return None


def evaluate_case(question: str, ranked_docs: list[str], expect_docs, error: str = "") -> CaseResult:
    """算一个问题的各项指标。"""
    expect = [str(x) for x in (expect_docs or [])]
    return CaseResult(
        question=question,
        ranked_docs=list(ranked_docs or []),
        expect_docs=expect,
        hit_at_1=hit_at_k(ranked_docs, expect, 1),
        hit_at_3=hit_at_k(ranked_docs, expect, 3),
        hit_at_5=hit_at_k(ranked_docs, expect, 5),
        rr=reciprocal_rank(ranked_docs, expect),
        error=error,
    )


@dataclass
class Summary:
    """一批问题的总体成绩。"""

    total: int = 0
    hit_at_1: float = 0.0
    hit_at_3: float = 0.0
    hit_at_5: float = 0.0
    mrr: float = 0.0
    errors: int = 0
    misses: list[CaseResult] = field(default_factory=list)

    def as_row(self) -> dict[str, str]:
        """给控制台表格用的一行。"""
        return {
            "问题数": str(self.total),
            "第一条即命中": f"{self.hit_at_1:.1%}",
            "前 3 条命中": f"{self.hit_at_3:.1%}",
            "前 5 条命中": f"{self.hit_at_5:.1%}",
            "MRR": f"{self.mrr:.3f}",
            "报错数": str(self.errors),
        }


def summarize(results: list[CaseResult]) -> Summary:
    """把逐题结果汇总成总体成绩。

    注意分母：**出错的问题也算在分母里**（按未命中计）。
    为什么不把它们剔出去 —— 剔出去会让成绩虚高：
    如果重排把一半请求打挂了，剩下的命中率看着反而更高，那就完全看不出问题了。
    """
    total = len(results)
    if total == 0:
        return Summary()
    misses = [r for r in results if not r.hit_at_5]
    return Summary(
        total=total,
        hit_at_1=sum(1 for r in results if r.hit_at_1) / total,
        hit_at_3=sum(1 for r in results if r.hit_at_3) / total,
        hit_at_5=sum(1 for r in results if r.hit_at_5) / total,
        mrr=sum(r.rr for r in results) / total,
        errors=sum(1 for r in results if r.error),
        misses=misses,
    )


def compare(before: Summary, after: Summary) -> list[str]:
    """对比两份成绩，返回人话描述（用来回答"改了之后到底变好没"）。"""
    lines = [
        f"第一条即命中：{before.hit_at_1:.1%} → {after.hit_at_1:.1%}"
        f"（{(after.hit_at_1 - before.hit_at_1) * 100:+.1f} 个百分点）",
        f"前 3 条命中 ：{before.hit_at_3:.1%} → {after.hit_at_3:.1%}"
        f"（{(after.hit_at_3 - before.hit_at_3) * 100:+.1f} 个百分点）",
        f"MRR        ：{before.mrr:.3f} → {after.mrr:.3f}（{after.mrr - before.mrr:+.3f}）",
    ]
    if after.errors > before.errors:
        lines.append(
            f"⚠️ 报错数增加：{before.errors} → {after.errors}，"
            "命中率变好也要先查清是参数真的更好，还是只是把报错的问题算少了"
        )
    return lines
