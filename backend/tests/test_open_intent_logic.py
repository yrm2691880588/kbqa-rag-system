"""开放性意图识别的 **Core 通用测试**。

## 这个文件和电商领域的测试有什么区别

- `app/domain/ecommerce/tests/test_open_intent.py`：测**电商词表**是否配置正确
  （"有哪些商品"要命中、"S10"要排除）。那组测试随领域走。
- 本文件：测**判断逻辑本身**是否正确，与具体词表无关。属于 Core。

## 怎么做到"与领域无关"

不假设任何具体词汇，而是：
1. 先取出当前领域配置的词表
2. 用人造输入验证逻辑关系（命中列举词且不含具体词 → True）

这样无论 `DOMAIN` 是 ecommerce 还是 finance，测试都成立。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.domain import get_domain, reset_for_testing
from app.services.open_intent import detect_open_intent, explain


class OpenIntentLogicTestCase(unittest.TestCase):
    """意图判断的**逻辑**（不涉及具体词表内容）。"""

    def setUp(self):
        # 每个测试前清缓存，确保读到的是当前 settings.domain
        reset_for_testing()

    def test_empty_input_never_open(self):
        """空串/纯空白永远不是开放意图。

        为什么单独测：空串若不短路，会被当作"没有具体词"而误判为列举，
        导致用户刚打开页面就收到一份清单。
        """
        self.assertFalse(detect_open_intent(""))
        self.assertFalse(detect_open_intent("   "))
        self.assertFalse(detect_open_intent("\n\t"))

    def test_question_without_any_keyword_is_not_open(self):
        """完全不包含列举词的句子，不应判为开放意图。"""
        self.assertFalse(detect_open_intent("这是一句普通的话，没有任何列举意图"))

    def test_logic_holds_for_currently_loaded_domain(self):
        """用当前领域的词表验证判断逻辑。

        构造两个输入：
          ① 只含列举词 → 应判为开放
          ② 列举词 + 具体词 → 应判为具体问题（被具体词否决）
        这样不依赖"商品"或"股票"这类具体词汇。
        """
        spec = get_domain()
        if not spec.open_intent_keywords or not spec.specific_hints:
            self.skipTest(f"领域 {spec.name} 词表不完整，跳过逻辑验证")

        kw = spec.open_intent_keywords[0]
        hint = spec.specific_hints[0]

        # ① 只有列举词
        self.assertTrue(
            detect_open_intent(kw),
            f"只含列举词 {kw!r} 应判为开放意图",
        )
        # ② 列举词 + 具体词 → 被否决
        self.assertFalse(
            detect_open_intent(f"{kw} {hint}"),
            f"列举词 {kw!r} 加具体词 {hint!r} 应判为具体问题",
        )


class OpenIntentExplainTestCase(unittest.TestCase):
    """explain() 的诊断输出（调参和排查误判时用）。"""

    def setUp(self):
        reset_for_testing()

    def test_explain_returns_expected_keys(self):
        r = explain("任意一句话")
        self.assertIn("is_open", r)
        self.assertIn("matched_keyword", r)
        self.assertIn("blocked_by_hint", r)

    def test_explain_reports_matched_keyword(self):
        spec = get_domain()
        if not spec.open_intent_keywords:
            self.skipTest("当前领域无列举词表")
        kw = spec.open_intent_keywords[0]
        r = explain(kw)
        self.assertEqual(r["matched_keyword"], kw)
        self.assertTrue(r["is_open"])

    def test_explain_reports_blocking_hint(self):
        spec = get_domain()
        if not spec.open_intent_keywords or not spec.specific_hints:
            self.skipTest("当前领域词表不完整")
        kw, hint = spec.open_intent_keywords[0], spec.specific_hints[0]
        r = explain(f"{kw} {hint}")
        self.assertEqual(r["blocked_by_hint"], hint)
        self.assertFalse(r["is_open"])


if __name__ == "__main__":
    unittest.main()
