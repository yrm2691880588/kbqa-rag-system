"""Code Review 修复项的回归测试。

覆盖这轮修的四个问题：

1. **检索链领域隔离**（P0）
   之前只给列表接口加了 domain 过滤，tenancy 里的四个函数没加。
   后果：财经领域能检索到电商库的内容（实测 kd_id 里出现了别领域的库）。

2. **自检轮数真正按配置执行**（P0）
   之前配 2 和配 1 行为一样（都只重做一次），配置形同虚设。

3. **build_retry_hint 的转义**（P1）
   之前用 `\\n` 字面量，模型收到的是一整行挤在一起的符号。

4. **OUT_OF_SCOPE 真正产生**（P2）
   之前枚举定义了但 route() 永远不返回，是死代码。

## 为什么这些测试重要

这四条都是"看起来实现了、实际没生效"的问题 ——
代码存在、测试全绿、但行为不符合配置和需求。
只有针对行为写断言才能发现。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.domain import load_domain
from app.services.answer_verify import build_retry_hint
from app.services.intent_router import Intent, route


class RetryHintEscapeTestCase(unittest.TestCase):
    """打回重做的提示词必须是**真的多行**。"""

    def test_contains_real_newline(self):
        hint = build_retry_hint("测试原因")
        self.assertIn(
            chr(10), hint,
            "提示词里没有真换行 —— 模型会看到一整行挤在一起的文字",
        )

    def test_no_literal_backslash_n(self):
        """不能出现字面量 \\n（反斜杠 + n 两个字符）。"""
        hint = build_retry_hint("测试原因")
        self.assertNotIn(
            chr(92) + "n", hint,
            "提示词里出现了字面量 \\n —— 转义写错了，模型看到的是符号而不是换行",
        )

    def test_contains_reason(self):
        """必须包含具体原因，否则模型不知道要改什么。"""
        reason = "数字与资料不一致"
        hint = build_retry_hint(reason)
        self.assertIn(reason, hint)

    def test_is_multiline(self):
        """至少 4 行，说明分段了。"""
        hint = build_retry_hint("原因")
        lines = [ln for ln in hint.split(chr(10)) if ln.strip()]
        self.assertGreaterEqual(len(lines), 4, "提示词没有分成多行")


class OutOfScopeTestCase(unittest.TestCase):
    """超范围判断：要拦住明显的，但绝不能误伤业务问题。"""

    def setUp(self):
        self.domain = load_domain("finance")

    def test_obvious_off_topic_blocked(self):
        """明显无关的问题应该被拦。"""
        cases = [
            "红烧肉怎么做",
            "番茄炒蛋的做法",
            "推荐个好看的电视剧",
            "帮我翻译成英文",
            "解方程 2x+3=7",
        ]
        for q in cases:
            with self.subTest(q=q):
                r = route(q, self.domain)
                self.assertEqual(
                    r.intent, Intent.OUT_OF_SCOPE,
                    f"「{q}」明显超出范围，应判 OUT_OF_SCOPE，实际 {r.intent}",
                )

    def test_business_questions_not_blocked(self):
        """业务问题绝不能被误判（比漏判严重得多）。"""
        cases = [
            "海联银行净利润多少",
            "报销制度怎么规定",
            "研发中心负责人是谁",
            "三家营收对比",
            "预算执行率多少",
            "启明科技毛利率",
            "这个费用能不能报",
            "审批流程是什么",
            "合同审批要多久",
        ]
        for q in cases:
            with self.subTest(q=q):
                r = route(q, self.domain)
                self.assertNotEqual(
                    r.intent, Intent.OUT_OF_SCOPE,
                    f"业务问题「{q}」被误判为超范围（原因：{r.reason}）",
                )

    def test_out_of_scope_has_reply(self):
        """拦截时必须给出回复文本，不能让用户看到空白。"""
        r = route("红烧肉怎么做", self.domain)
        if r.intent == Intent.OUT_OF_SCOPE:
            self.assertTrue(r.reply, "超范围分支没有给出回复文本")

    def test_domain_without_scope_never_blocks(self):
        """领域没配 scope_description 时不该做超范围判断。"""
        from app.domain.base import DomainSpec

        spec = DomainSpec(
            name="tmp_test", display_name="测试",
            persona="你是测试助手", audience="测试者",
            scope_description="",   # 没配
        )
        r = route("红烧肉怎么做", spec)
        self.assertNotEqual(
            r.intent, Intent.OUT_OF_SCOPE,
            "没配范围说明的领域不该拦任何问题",
        )


class DomainIsolationQueryTestCase(unittest.TestCase):
    """tenancy 里的查询必须带领域条件。"""

    def test_accessible_kb_ids_filters_domain(self):
        """accessible_kb_ids 的 SQL 里必须有 domain 条件。

        为什么测这个：
            之前只给列表接口加了过滤，这里没加，
            导致检索链能拿到别的领域的库 id。
        """
        import inspect

        from app.core import tenancy

        src = inspect.getsource(tenancy.accessible_kb_ids)
        self.assertIn(
            "KnowledgeBase.domain", src,
            "accessible_kb_ids 没有按 domain 过滤 —— 检索链会串领域",
        )

    def test_get_scoped_kb_checks_domain(self):
        """按 id 取单个库时也要校验领域。"""
        import inspect

        from app.core import tenancy

        src = inspect.getsource(tenancy.get_scoped_kb)
        self.assertIn(
            "get_domain()", src,
            "get_scoped_kb 没有校验领域 —— 猜 id 就能读到别领域的库",
        )

    def test_org_scope_filters_domain(self):
        """对外 API 的组织范围同样要按领域过滤。"""
        import inspect

        from app.core import tenancy

        src = inspect.getsource(tenancy.accessible_kb_ids_for_org)
        self.assertIn(
            "KnowledgeBase.domain", src,
            "accessible_kb_ids_for_org 没有按 domain 过滤",
        )


class VerifyRoundsLoopTestCase(unittest.TestCase):
    """自检必须按配置轮数循环，不能写死一次。"""

    def test_rag_has_verify_loop(self):
        """rag.py 里要有基于 answer_verify_rounds 的循环。

        为什么测这个：
            之前写死"只重做一次"，配 1 和配 2 行为完全一样，
            配置字段形同虚设。
        """
        import inspect

        from app.services import rag

        src = inspect.getsource(rag.stream_answer)

        # 必须读取配置值
        self.assertIn(
            "answer_verify_rounds", src,
            "stream_answer 没有使用 answer_verify_rounds 配置",
        )

        # 必须有 while 或 for 循环（而不是固定的两次调用）
        has_loop = "while " in src or "for " in src
        self.assertTrue(has_loop, "自检没有循环结构 —— 配置的轮数不会生效")


class VerifyPromptNeutralTestCase(unittest.TestCase):
    """Core 的核查提示词不能写死某个行业的术语。"""

    def test_verify_prompt_has_no_domain_terms(self):
        """ADR-0001：Core 不能出现领域专有名词。

        最初写的是"回答是否答非所问（问净利润却答营收）" ——
        换医疗领域就必须改 Core 代码。
        """
        from app.services.answer_verify import _VERIFY_SYSTEM

        forbidden = ["净利润", "营收", "毛利率", "股价", "商品", "诊断"]
        for w in forbidden:
            with self.subTest(word=w):
                self.assertNotIn(
                    w, _VERIFY_SYSTEM,
                    f"核查提示词里出现了行业术语「{w}」—— 换领域就得改 Core",
                )


if __name__ == "__main__":
    unittest.main()
