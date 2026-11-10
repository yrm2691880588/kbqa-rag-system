"""检索评估指标测试（P2-21）。

这个文件在测什么（大白话）：
    评估指标的用处是"用数字判断检索改好了还是改坏了"。
    但如果指标本身算错了，那比没有指标更危险 ——
    你会拿着一个错的数字，信心十足地做出错误的决定。

    这里最在意的一条是：**跑失败的问题必须算进分母**。
    如果"出错的问题"被悄悄剔掉，那么把重排调成"一半请求报错"，
    剩下的那些问题命中率反而更高，成绩看起来一片大好 ——
    这就是典型的"用指标把自己骗了"。

运行方式（backend 目录下）：
    python -m unittest tests.test_eval_metrics -v
"""

import json
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.eval_metrics import (  # noqa: E402
    CaseResult, compare, evaluate_case, hit_at_k, reciprocal_rank, summarize,
)

BACKEND = pathlib.Path(__file__).resolve().parent.parent


class HitAtKTestCase(unittest.TestCase):
    def test_found_within_k(self):
        self.assertTrue(hit_at_k(["A", "B", "C"], ["C"], k=3))
        self.assertFalse(hit_at_k(["A", "B", "C"], ["C"], k=2), "第 3 位才命中，看前 2 位不算")
        self.assertTrue(hit_at_k(["A"], ["A"], k=1))

    def test_any_of_expected_counts(self):
        """期望资料有多篇时，命中任意一篇就算命中。"""
        self.assertTrue(hit_at_k(["X", "B"], ["A", "B"], k=2))

    def test_case_and_whitespace_insensitive(self):
        """文件名大小写、前后空格不该导致误判"没命中"。

        这条防的是"指标看起来很差不代表真的差"：资料叫 "Manual.PDF"，
        评估集里写成 "manual.pdf"，如果不做归一化就会被算成未命中，
        然后我们会去瞎调参数（其实检索一点问题都没有）。
        """
        self.assertTrue(hit_at_k(["  Manual.PDF "], ["manual.pdf"], k=1))
        self.assertTrue(hit_at_k(["manual.pdf"], ["  MANUAL.PDF  "], k=1))

    def test_k_zero_is_false(self):
        self.assertFalse(hit_at_k(["A"], ["A"], k=0))

    def test_empty_inputs(self):
        self.assertFalse(hit_at_k([], ["A"], k=3))
        self.assertFalse(hit_at_k(["A"], [], k=3), "没写期望资料时不该算命中")


class ReciprocalRankTestCase(unittest.TestCase):
    def test_rank_values(self):
        """第 1 位=1.0，第 2 位=0.5，第 4 位=0.25，没命中=0。"""
        self.assertAlmostEqual(reciprocal_rank(["A", "B"], ["A"]), 1.0)
        self.assertAlmostEqual(reciprocal_rank(["A", "B"], ["B"]), 0.5)
        self.assertAlmostEqual(reciprocal_rank(["a", "b", "c", "D"], ["d"]), 0.25)
        self.assertAlmostEqual(reciprocal_rank(["A", "B"], ["Z"]), 0.0)

    def test_empty_expect_is_zero(self):
        self.assertAlmostEqual(reciprocal_rank(["A"], []), 0.0)


class EvaluateCaseTestCase(unittest.TestCase):
    def test_matched_rank_is_reported(self):
        """要能直接看出"期望资料排在第几位"，排查时这是最有用的一个数。"""
        r = evaluate_case("问题", ["A", "B", "C"], ["C"])
        self.assertEqual(r.matched_rank, 3)
        self.assertFalse(r.hit_at_1)
        self.assertTrue(r.hit_at_3)

    def test_matched_rank_none_when_missing(self):
        r = evaluate_case("问题", ["A", "B"], ["Z"])
        self.assertIsNone(r.matched_rank)
        self.assertFalse(r.hit_at_5)
        self.assertAlmostEqual(r.rr, 0.0)

    def test_error_is_recorded(self):
        r = evaluate_case("问题", [], ["A"], error="ConnectionError: 连不上")
        self.assertEqual(r.error, "ConnectionError: 连不上")
        self.assertFalse(r.hit_at_1)


class SummarizeTestCase(unittest.TestCase):
    def test_basic_rates(self):
        results = [
            evaluate_case("q1", ["A"], ["A"]),            # 第 1 位命中 → 1.0
            evaluate_case("q2", ["X", "B"], ["B"]),       # 第 2 位命中 → 0.5
            evaluate_case("q3", ["X", "Y", "C"], ["C"]),  # 第 3 位命中
            evaluate_case("q4", ["X", "Y"], ["Z"]),       # 未命中
        ]
        s = summarize(results)
        self.assertEqual(s.total, 4)
        self.assertAlmostEqual(s.hit_at_1, 0.25)
        self.assertAlmostEqual(s.hit_at_3, 0.75)
        self.assertAlmostEqual(s.hit_at_5, 0.75)
        self.assertAlmostEqual(s.mrr, (1.0 + 0.5 + 1 / 3 + 0.0) / 4)
        self.assertEqual(s.errors, 0)
        self.assertEqual(len(s.misses), 1)

    def test_errors_stay_in_denominator(self):
        """**关键**：报错的问题必须计入分母（按未命中算）。

        如果不计入，就会出现这种荒唐结果：让重排把一半请求打挂，
        剩下的命中率反而更高，报表一片绿 —— 用错的指标骗自己。
        """
        results = [
            evaluate_case("q1", ["A"], ["A"]),
            evaluate_case("q2", [], ["B"], error="Timeout: 重排超时"),
            evaluate_case("q3", [], ["C"], error="Timeout: 重排超时"),
            evaluate_case("q4", [], ["D"], error="Timeout: 重排超时"),
        ]
        s = summarize(results)
        self.assertEqual(s.total, 4, "报错的问题被剔出分母了，成绩会虚高")
        self.assertAlmostEqual(s.hit_at_1, 0.25, "命中率必须按 4 个问题算，不是按 1 个")
        self.assertEqual(s.errors, 3)

    def test_empty_returns_zero_summary(self):
        s = summarize([])
        self.assertEqual(s.total, 0)
        self.assertAlmostEqual(s.mrr, 0.0)
        self.assertEqual(s.misses, [])

    def test_row_shape(self):
        """控制台表格要的键都在（少一个都会 KeyError 崩在打印那一步）。"""
        row = summarize([evaluate_case("q", ["A"], ["A"])]).as_row()
        for key in ("问题数", "第一条即命中", "前 3 条命中", "前 5 条命中", "MRR", "报错数"):
            self.assertIn(key, row)


class CompareTestCase(unittest.TestCase):
    def test_reports_direction(self):
        before = summarize([evaluate_case(f"q{i}", ["X"], ["A"]) for i in range(4)])
        after = summarize([evaluate_case(f"q{i}", ["A"], ["A"]) for i in range(4)])
        lines = "\n".join(compare(before, after))
        self.assertIn("第一条即命中", lines)
        self.assertIn("+100.0 个百分点", lines)

    def test_warns_when_errors_increase(self):
        """报错变多时必须警告 —— 否则"命中率涨了"可能是把问题算少了造成的假象。"""
        before = summarize([evaluate_case("q1", ["A"], ["A"])])
        after = summarize([
            evaluate_case("q1", ["A"], ["A"]),
            evaluate_case("q2", [], ["A"], error="超时"),
        ])
        lines = "\n".join(compare(before, after))
        self.assertIn("报错数增加", lines)


class EvalSetFileTestCase(unittest.TestCase):
    """评估集文件本身也要检查（格式写错的话，脚本跑起来只会报个看不懂的错）。"""

    def _load(self, name: str):
        path = BACKEND / "eval" / name
        if not path.exists():
            self.skipTest(f"{name} 不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_example_set_is_valid(self):
        data = self._load("retrieval_eval_set.example.json")
        cases = data.get("cases")
        self.assertIsInstance(cases, list)
        self.assertGreater(len(cases), 0, "模板里一个问题都没有，别人复制了也不知道怎么填")
        for c in cases:
            self.assertTrue(str(c.get("question", "")).strip(), f"有 case 没写问题：{c}")
            expect = c.get("expect_docs")
            self.assertIsInstance(expect, list)
            self.assertTrue(expect, f"有 case 没写期望资料：{c}")
            for name in expect:
                self.assertTrue(str(name).strip(), f"期望资料里有空值：{c}")


if __name__ == "__main__":
    unittest.main()
