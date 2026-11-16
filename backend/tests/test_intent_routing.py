"""意图分流与答案自检的测试。

## 测什么

1. **分流**：不同问题走不同的路（闲聊/违规/实时数据/知识库）
2. **自检**：不合格的答案会被判不通过
3. **兜底**：分流和自检失败时不能把主流程搞挂

## 为什么这些测试重要

分流的关键风险是**误判**：
  · 把正常问题判成"超范围" → 用户被无辜拒答，比答错更气人
  · 把违规问题放过 → 给出不该给的建议（合规风险）

自检的关键风险是**过度严格**：
  · 把好答案判成不合格 → 触发重做 → 浪费调用还可能越改越差

所以测试里既验证"该拦的拦住"，也验证"不该拦的别乱拦"。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.domain import get_domain, load_domain
from app.services.intent_router import Intent, route


class SmalltalkRoutingTestCase(unittest.TestCase):
    """闲聊类问题不该去查知识库。"""

    def test_greeting_routed_to_smalltalk(self):
        d = get_domain()
        for q in ("你好", "您好", "hi", "在吗"):
            r = route(q, d)
            self.assertEqual(
                r.intent, Intent.SMALLTALK,
                f"问候语「{q}」应走闲聊分支，实际走 {r.intent}",
            )
            self.assertTrue(r.reply, "闲聊分支必须给出回复文本")

    def test_whoami_routed_to_smalltalk(self):
        d = get_domain()
        r = route("你是谁", d)
        self.assertEqual(r.intent, Intent.SMALLTALK)


class RealtimeRoutingTestCase(unittest.TestCase):
    """实时数据类问题必须拦住（知识库里不可能有）。"""

    def test_weather_is_realtime(self):
        d = get_domain()
        r = route("明天天气怎么样", d)
        self.assertEqual(r.intent, Intent.NEED_EXTERNAL)

    def test_weather_reply_gives_alternative(self):
        """拒答天气时要给出替代方案，而不是一句话打死。"""
        d = get_domain()
        r = route("明天适合钓鱼吗", d)
        self.assertEqual(r.intent, Intent.NEED_EXTERNAL)
        # 回复里应该有"知识库"相关的引导，告诉用户还能问什么
        self.assertIn("知识库", r.reply)

    def test_stock_price_is_handled(self):
        """股价既涉及实时数据也涉及禁令，两类拦截都接受。"""
        d = load_domain("finance")
        r = route("茅台现在股价多少", d)
        self.assertIn(
            r.intent, (Intent.NEED_EXTERNAL, Intent.BLOCKED),
            f"股价类问题应被拦截，实际走 {r.intent}",
        )


class ForbiddenRoutingTestCase(unittest.TestCase):
    """违反领域禁令的问题必须拒答。"""

    def test_predict_price_blocked(self):
        d = load_domain("finance")
        for q in ("帮我预测下茅台股价", "预测一下明天涨跌", "该买哪只股票", "推荐一只股票"):
            r = route(q, d)
            self.assertEqual(
                r.intent, Intent.BLOCKED,
                f"「{q}」违反财经禁令，应判 BLOCKED，实际 {r.intent}",
            )

    def test_no_forbidden_means_no_block(self):
        """没配禁令的领域不该因为这套规则误拦。"""
        d = load_domain("generic")
        r = route("帮我预测下明天的销量", d)
        self.assertNotEqual(r.intent, Intent.BLOCKED)


class KnowledgeRoutingTestCase(unittest.TestCase):
    """正常业务问题必须放行 —— 这是最容易误伤的地方。"""

    def test_normal_questions_pass(self):
        d = load_domain("finance")
        cases = [
            "海联银行2025年的净利润是多少",
            "启明科技的毛利率怎么样",
            "长风能源的资产负债率高不高",
            "市盈率是什么意思",
            "这几家谁的利润最好",
        ]
        for q in cases:
            r = route(q, d)
            self.assertEqual(
                r.intent, Intent.KNOWLEDGE,
                f"正常业务问题「{q}」被误判为 {r.intent}（原因：{r.reason}）",
            )

    def test_empty_question_defaults_to_knowledge(self):
        d = get_domain()
        self.assertEqual(route("", d).intent, Intent.KNOWLEDGE)


class RouterRobustnessTestCase(unittest.TestCase):
    """分流本身不能成为故障点。"""

    def test_route_never_raises(self):
        d = get_domain()
        # 各种奇怪输入都不应抛异常
        for q in ("", " ", "？", "a" * 5000, "你好" * 500):
            try:
                route(q, d)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"分流对输入 {q[:20]!r} 抛异常：{exc}")

    def test_all_intents_have_handler(self):
        """每个意图值都应该有明确的处理方式（不能有漏网的值）。"""
        expected = {
            Intent.SMALLTALK, Intent.BLOCKED,
            Intent.NEED_EXTERNAL, Intent.KNOWLEDGE, Intent.OUT_OF_SCOPE,
        }
        self.assertEqual(set(Intent), expected)


class DomainConfigTestCase(unittest.TestCase):
    """领域字段的默认值要安全。"""

    def test_default_verify_rounds_is_zero(self):
        """默认不开自检 —— 加字段不改变现有领域的行为。"""
        from app.domain.base import DomainSpec

        spec = DomainSpec(
            name="tmp_test", display_name="测试", persona="你是测试助手",
            audience="测试者",
        )
        self.assertEqual(spec.answer_verify_rounds, 0)

    def test_finance_has_verify_enabled(self):
        d = load_domain("finance")
        self.assertGreater(
            d.answer_verify_rounds, 0,
            "财经领域答错代价高，应该开启自检",
        )

    def test_finance_disallows_external_facts(self):
        d = load_domain("finance")
        self.assertFalse(
            d.allow_external_facts,
            "财经领域绝不允许模型用通用知识补充（会编数字）",
        )

    def test_generic_has_no_verify(self):
        d = load_domain("generic")
        self.assertEqual(d.answer_verify_rounds, 0, "通用领域不该额外增加调用成本")


if __name__ == "__main__":
    unittest.main()
