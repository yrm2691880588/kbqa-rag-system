"""财经领域的意图识别测试。

**本文件属于 finance 领域**：用例里的「股票」「市盈率」「海联银行」都是财经词。
导出财经项目时，这个文件会跟着一起走。

判断逻辑在 Core（app/services/open_intent.py），
词表与禁令在 app/domain/finance/domain.py。

运行：
    python -m unittest app.domain.finance.tests.test_open_intent -v
"""

import os
import sys
import unittest

# 测的是财经领域，必须先锁定领域，否则会加载 generic 兜底
os.environ.setdefault("DOMAIN", "finance")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
))))

from app.domain import reset_for_testing
from app.services.open_intent import detect_open_intent, explain


class FinanceOpenIntentTestCase(unittest.TestCase):
    """财经场景：列举意图 vs 具体指标问题。"""

    def setUp(self):
        reset_for_testing()

    # ---------- 应判为「列举」 ----------

    def test_stock_list_questions(self):
        """问「有哪些股票」这类要清单的问题，应判为开放意图。"""
        for q in ("有哪些股票", "有什么股票", "股票列表", "全部股票"):
            with self.subTest(q=q):
                self.assertTrue(detect_open_intent(q), f"{q!r} 应判为列举")

    def test_fund_list_questions(self):
        for q in ("有哪些基金", "基金列表", "所有基金"):
            with self.subTest(q=q):
                self.assertTrue(detect_open_intent(q), f"{q!r} 应判为列举")

    def test_bare_introduce(self):
        """只说「介绍一下」—— 财经词表里也有这个词，应判为列举。"""
        self.assertTrue(detect_open_intent("介绍一下"))

    # ---------- 应判为「具体问题」 ----------

    def test_specific_ticker_not_list(self):
        """带具体标的名称时不能列清单，要走检索。"""
        for q in ("介绍一下海联银行", "启明科技的营收", "长风能源怎么样"):
            with self.subTest(q=q):
                self.assertFalse(detect_open_intent(q), f"{q!r} 应判为具体问题")

    def test_financial_metric_not_list(self):
        """问具体财务指标时不能列清单。"""
        for q in (
            "市盈率是多少",
            "净利润同比增长多少",
            "净资产收益率怎么样",
            "股价最近怎么样",
            "年报里怎么说的",
        ):
            with self.subTest(q=q):
                self.assertFalse(detect_open_intent(q), f"{q!r} 应判为具体问题")

    def test_cross_domain_blocked(self):
        """跨领域词应被 foreign_hints 拦住。

        这是真实发现过的缺陷：财经词表里有通用的「介绍一下」，
        若不拦，问「介绍一下本商品」会给用户列一份股票清单，完全答非所问。
        """
        for q in ("介绍一下本商品", "介绍一下手机", "有哪些商品", "商品保修多久"):
            with self.subTest(q=q):
                self.assertFalse(
                    detect_open_intent(q),
                    f"{q!r} 含电商词，不应在财经领域被判为列举",
                )

    def test_cross_domain_block_reason_is_reported(self):
        """explain() 要能说清是哪个词拦住的，便于调参。"""
        r = explain("介绍一下本商品")
        self.assertFalse(r["is_open"])
        self.assertTrue(
            r["blocked_by_foreign"],
            f"应由 foreign_hints 拦住，实际 explain 结果：{r}",
        )


class FinanceComplianceTestCase(unittest.TestCase):
    """合规禁令的**配置正确性**（不是模型行为）。

    为什么只测配置：
        禁令是否被模型真正遵守，属于概率性行为，自动化测试无法可靠断言。
        这里只保证「禁令写进去了、写具体了」，实际效果靠人工验收清单。
    """

    def setUp(self):
        reset_for_testing()

    def test_forbidden_rules_exist(self):
        from app.domain import get_domain

        d = get_domain()
        self.assertGreaterEqual(
            len(d.forbidden), 3,
            "财经领域至少要有 3 条合规禁令（不能只说'不要违规'）",
        )

    def test_forbidden_rules_are_specific(self):
        """禁令必须提到具体的禁止对象，不能是空话。

        检查关键词：预测/推荐/承诺/注明/顾问 这类可执行的动词。
        """
        from app.domain import get_domain

        text = "".join(get_domain().forbidden)
        for kw in ("预测", "推荐", "承诺"):
            self.assertIn(kw, text, f"禁令里应明确提到「{kw}」")

    def test_no_hit_message_is_domain_specific(self):
        """无命中提示语要提到「财经」，不能是笼统的「没找到资料」。"""
        from app.domain import get_domain

        d = get_domain()
        self.assertIn("财经", d.no_hit_message)


if __name__ == "__main__":
    unittest.main()
