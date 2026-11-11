"""AI 调用治理测试：限流、熔断、问题缓存。

这个文件在测什么（大白话）：
    三样东西都是为了"高峰别崩、成本别乱"，但每样都有一个容易做错的地方：

    ① **限流**：桶要能"随时间恢复"，否则用满一次就永远用不了；
       也要能把不同客户分开计数，否则一家把量用光，别家都被连累。

    ② **熔断**：状态机三个状态之间来回切，最容易写错两处 ——
       "跳闸后到点没恢复"（保险丝烧了不换新的）、
       "试探失败没有重新跳闸"（半死不活时反复放流量进去）。
       这两处出错的表现都是"网站看着正常但一直报错"，很难查。

    ③ **缓存**：全文件最重要的一条是**隔离**。
       缓存键如果没带"是谁在问"，企业 A 问过的问题，企业 B 问同样的话
       就会拿到 A 的答案 —— 这是跨客户数据泄露，比多花点钱严重一万倍。
       所以这里有一条专门的"不同客户绝不共用缓存"的断言。

    还守一条通用原则：**任何治理机制都不该把功能搞挂**。
    限流/熔断没配置时（默认状态），一切必须和没有这套东西时完全一样。

运行方式（backend 目录下）：
    python -m unittest tests.test_ai_guard -v
"""

import asyncio
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.services import ai_guard as ag


def _run(coro):
    return asyncio.run(coro)


class FakeClock:
    """可控时钟：测试里不用真的等 60 秒，把时间"往前拨"就行。"""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ============================================================
# ① 限流
# ============================================================


class RateLimiterTestCase(unittest.TestCase):
    def setUp(self):
        self.limiter = ag.RateLimiter(per_minute=0)

    def test_disabled_by_default(self):
        """默认不限流：治理机制不能改变默认行为（否则升级就出事）。"""
        self.limiter.configure(0)
        for _ in range(1000):
            self.assertTrue(self.limiter.allow("org:1"))

    def test_allows_up_to_capacity_then_blocks(self):
        """每分钟 5 次：前 5 次放行，第 6 次拦下。"""
        self.limiter.configure(5)
        clock = FakeClock()
        results = [self.limiter.allow("org:1", now=clock()) for _ in range(6)]
        self.assertEqual(results, [True] * 5 + [False])

    def test_refills_over_time(self):
        """桶要随时间恢复 —— 否则"用满一次就永远用不了了"。"""
        self.limiter.configure(60)      # 每秒补 1 个
        clock = FakeClock()
        for _ in range(60):
            self.assertTrue(self.limiter.allow("org:1", now=clock()))
        self.assertFalse(self.limiter.allow("org:1", now=clock()), "桶满了却还放行")

        clock.advance(1.0)              # 过 1 秒，应补回 1 个
        self.assertTrue(self.limiter.allow("org:1", now=clock()))
        self.assertFalse(self.limiter.allow("org:1", now=clock()))

    def test_orgs_are_counted_separately(self):
        """不同客户分开计数：一家用满不能连累别家。"""
        self.limiter.configure(2)
        clock = FakeClock()
        self.assertTrue(self.limiter.allow("org:1", now=clock()))
        self.assertTrue(self.limiter.allow("org:1", now=clock()))
        self.assertFalse(self.limiter.allow("org:1", now=clock()), "org:1 该被限了")
        self.assertTrue(self.limiter.allow("org:2", now=clock()), "org:2 不该被 org:1 连累")

    def test_retry_after_is_meaningful(self):
        """要告诉用户"等几秒"，而不是只甩一句"太频繁"。"""
        self.limiter.configure(60)
        clock = FakeClock()
        for _ in range(60):
            self.limiter.allow("org:1", now=clock())
        wait = self.limiter.retry_after_seconds("org:1", now=clock())
        self.assertGreaterEqual(wait, 1, "等 0 秒的就等于没提示")
        self.assertLessEqual(wait, 5, f"补偿速度是每秒 1 个，不该要等这么久：{wait}")

        # 桶是满的时候不该说要等
        self.assertEqual(self.limiter.retry_after_seconds("org:fresh", now=clock()), 0)


# ============================================================
# ② 熔断
# ============================================================


class CircuitBreakerTestCase(unittest.TestCase):
    def setUp(self):
        self.cb = ag.CircuitBreaker(fail_threshold=3, open_seconds=30, half_open_trials=1)

    def test_closed_until_threshold(self):
        """没到阈值前照常放行（不能一失败就跳闸，偶发抖动是正常的）。"""
        for _ in range(2):
            self.assertTrue(self.cb.allow())
            self.cb.record_failure()
        self.assertEqual(self.cb.state, ag.CLOSED)
        self.assertTrue(self.cb.allow(), "才失败 2 次（阈值 3）就不放行了")

    def test_opens_at_threshold_and_blocks(self):
        """到阈值跳闸，之后立刻快速失败（不真的去调外部）。"""
        for _ in range(3):
            self.cb.record_failure()
        self.assertEqual(self.cb.state, ag.OPEN)
        self.assertFalse(self.cb.allow(), "跳闸后还在放流量进去，等于没熔断")

    def test_half_open_after_cooldown_then_recovers(self):
        """冷却结束 → 试探 → 成功 → 恢复正常。

        注意：跳闸时间和判断时间必须来自**同一个时钟**。
        混用（一边用真实时间记跳闸、一边拿假时钟去判断）会得到
        "冷却早就过了却还在拦"的假象 —— 我第一次就写成这样，
        白白查了一轮。所以下面所有 record_* / allow 都显式传同一个 clock。
        """
        clock = FakeClock()
        for _ in range(3):
            self.cb.record_failure(now=clock())
        self.assertEqual(self.cb.state, ag.OPEN)

        self.assertFalse(self.cb.allow(now=clock()), "冷却期内不该放行")
        clock.advance(31)
        self.assertTrue(self.cb.allow(now=clock()), "冷却结束应放一个试探请求")
        self.cb.record_trial()
        # 试探名额只有 1 个，第二个请求要等结果
        self.assertFalse(self.cb.allow(now=clock()), "试探名额用完后还在放流量")

        self.cb.record_success()
        self.assertEqual(self.cb.state, ag.CLOSED)
        self.assertTrue(self.cb.allow(now=clock()), "恢复后应正常放行")

    def test_half_open_failure_reopens(self):
        """**关键**：试探失败要立刻重新跳闸。

        不这么做的话（比如只是把失败次数加一），外部服务"半死不活"时
        会反复放流量进去，熔断器等于摆设。
        """
        clock = FakeClock()
        for _ in range(3):
            self.cb.record_failure(now=clock())
        clock.advance(31)
        self.assertTrue(self.cb.allow(now=clock()))
        self.cb.record_trial()
        self.cb.record_failure(now=clock())          # 试探失败
        self.assertEqual(self.cb.state, ag.OPEN, "试探失败后没有重新跳闸")
        self.assertFalse(self.cb.allow(now=clock()), "重新跳闸后立刻又在放流量")

        # 而且冷却时间要重新开始算（不是沿用上一次的时间戳）
        clock.advance(5)
        self.assertFalse(self.cb.allow(now=clock()), "冷却时间没有重新计时")

    def test_success_resets_failure_count(self):
        """成功一次要把失败计数清零，否则偶发失败会慢慢攒到阈值。"""
        self.cb.record_failure()
        self.cb.record_failure()
        self.cb.record_success()
        self.assertEqual(self.cb.failure_count, 0)

        self.cb.record_failure()
        self.assertTrue(self.cb.allow(), "计数没清零，攒够阈值就误跳闸了")

    def test_disabled_breaker_never_blocks(self):
        """关掉熔断时就完全不管（配置说了算，不能偷偷生效）。"""
        cb = ag.CircuitBreaker(fail_threshold=1, enabled=False)
        for _ in range(50):
            cb.record_failure()
            self.assertTrue(cb.allow())


# ============================================================
# ③ 问题缓存（隔离是重中之重）
# ============================================================


class CacheKeyTestCase(unittest.TestCase):
    def test_same_question_same_key(self):
        k1 = ag.make_cache_key("公司的地址在哪？", [1, 2], "org:1")
        k2 = ag.make_cache_key("  公司的地址在哪？  ", [2, 1], "org:1")
        self.assertEqual(k1, k2, "空白/顺序不同就该命中同一条缓存（否则等于没缓存）")

    def test_scope_must_differ(self):
        """**全文件最重要的一条**：不同客户问同样的问题，缓存键必须不同。

        如果这条不成立，企业 A 问过的问题，企业 B 问同样的话
        就会拿到 A 的答案 —— 跨客户数据泄露。
        这比"多花一次模型调用"严重一万倍，所以单独钉死。
        """
        q = "公司的地址在哪？"
        k_a = ag.make_cache_key(q, [1], "org:1")
        k_b = ag.make_cache_key(q, [1], "org:2")
        self.assertNotEqual(k_a, k_b, "不同组织的同问题共用了缓存键 —— 会串答案")

    def test_different_kb_scope_differs(self):
        """可见知识库不同也不能共用（同一组织内，不同库范围可能答案不同）。"""
        k1 = ag.make_cache_key("价格多少", [1], "org:1")
        k2 = ag.make_cache_key("价格多少", [1, 2], "org:1")
        k3 = ag.make_cache_key("价格多少", None, "org:1")
        self.assertEqual(len({k1, k2, k3}), 3)

    def test_none_and_empty_scope_are_different(self):
        """None（不限制）和 []（什么都看不到）必须区分 —— 本项目踩过两次的坑。"""
        self.assertNotEqual(
            ag.normalize_scope(None),
            ag.normalize_scope([]),
            "「不限制」和「一个都看不到」被当成了同一件事",
        )

    def test_normalize_question(self):
        self.assertEqual(ag.normalize_question("  A   B  "), "a b")
        self.assertEqual(ag.normalize_question("你好"), "你好")


class QuestionCacheTestCase(unittest.TestCase):
    def test_disabled_by_default(self):
        c = ag.QuestionCache(enabled=False)
        c.put("k", {"answer": "a"})
        self.assertIsNone(c.get("k"), "默认关闭时不该命中（缓存要先确认业务能接受）")

    def test_put_and_get(self):
        c = ag.QuestionCache(ttl_seconds=60, max_entries=10, enabled=True)
        clock = FakeClock()
        c.put("k", {"answer": "答案"}, now=clock())
        self.assertEqual(c.get("k", now=clock()), {"answer": "答案"})

    def test_ttl_expiry(self):
        """过期的缓存不能再返回（知识库更新后不能一直答老话）。"""
        c = ag.QuestionCache(ttl_seconds=60, max_entries=10, enabled=True)
        clock = FakeClock()
        c.put("k", {"answer": "老答案"}, now=clock())
        clock.advance(61)
        self.assertIsNone(c.get("k", now=clock()), "过期缓存还在返回")

    def test_lru_eviction(self):
        """超出上限时淘汰最久没被用到的（不然被刷奇葩问题就把内存吃光了）。"""
        c = ag.QuestionCache(ttl_seconds=600, max_entries=3, enabled=True)
        clock = FakeClock()
        for i in range(3):
            c.put(f"k{i}", {"answer": i}, now=clock())
        c.get("k0", now=clock())              # 让 k0 变成"最近用过"
        c.put("k3", {"answer": 3}, now=clock())   # 触发淘汰

        self.assertIsNotNone(c.get("k0", now=clock()), "最久未用的应该是 k1/k2 而不是 k0")
        self.assertIsNone(c.get("k1", now=clock()), "最久未用的那条没被淘汰")

    def test_hit_counting(self):
        """命中率要能统计出来（不然没法判断开缓存到底有没有用）。"""
        c = ag.QuestionCache(ttl_seconds=600, max_entries=10, enabled=True)
        c.put("k", {"answer": "a"})
        c.get("k")
        c.get("miss")
        st = c.stats()
        self.assertEqual(st["hits"], 1)
        self.assertEqual(st["misses"], 1)
        self.assertAlmostEqual(st["hit_rate"], 0.5)


# ============================================================
# ④ 配置应用（"配了要生效"，否则等于没配）
# ============================================================


class ReloadFromSettingsTestCase(unittest.TestCase):
    def test_reload_applies_all_three(self):
        old = (settings.ai_rate_limit_per_minute, settings.ai_cb_enabled,
               settings.ai_cb_fail_threshold, settings.ai_cb_open_seconds,
               settings.qa_cache_enabled, settings.qa_cache_ttl_seconds,
               settings.qa_cache_max_entries)
        try:
            settings.ai_rate_limit_per_minute = 42
            settings.ai_cb_enabled = True
            settings.ai_cb_fail_threshold = 7
            settings.ai_cb_open_seconds = 90
            settings.qa_cache_enabled = True
            settings.qa_cache_ttl_seconds = 120
            settings.qa_cache_max_entries = 33
            ag.reload_from_settings()

            self.assertEqual(ag.rate_limiter.per_minute, 42)
            self.assertEqual(ag.llm_breaker.fail_threshold, 7)
            self.assertEqual(ag.llm_breaker.open_seconds, 90)
            self.assertTrue(ag.question_cache.enabled)
            self.assertEqual(ag.question_cache.ttl_seconds, 120)
            self.assertEqual(ag.question_cache.max_entries, 33)
        finally:
            (settings.ai_rate_limit_per_minute, settings.ai_cb_enabled,
             settings.ai_cb_fail_threshold, settings.ai_cb_open_seconds,
             settings.qa_cache_enabled, settings.qa_cache_ttl_seconds,
             settings.qa_cache_max_entries) = old
            ag.reload_from_settings()


# ============================================================
# ⑤ 接到问答链路上（缓存命中要真的省掉检索和模型调用）
# ============================================================


class StreamAnswerCacheTestCase(unittest.TestCase):
    """验证"缓存真的接上了"，而不只是"缓存类本身能跑"。

    只测缓存类是不够的：把缓存类写得再对，如果检索流程里根本没查缓存，
    它就是个摆设 —— 而且不报错、看不出来。
    """

    def setUp(self):
        self.old_enabled = settings.qa_cache_enabled
        self.old_ttl = settings.qa_cache_ttl_seconds
        ag.question_cache.clear()

    def tearDown(self):
        settings.qa_cache_enabled = self.old_enabled
        settings.qa_cache_ttl_seconds = self.old_ttl
        ag.question_cache.clear()

    def _collect(self, gen):
        async def run():
            return [evt async for evt in gen]
        return _run(run())

    def _patch_retrieval(self, hits):
        """把检索换成固定结果，避免测试依赖向量库。"""
        from app.services import rag as rag_mod

        async def fake_retrieve(question, kb_ids=None, top_k=None):
            return [dict(h) for h in hits]

        return unittest.mock.patch.object(rag_mod, "retrieve", fake_retrieve)

    def test_cache_writes_then_hits_without_retrieval(self):
        """第一次真查、第二次命中缓存，且**第二次没有再去检索**。"""
        from app.services import rag as rag_mod

        settings.qa_cache_enabled = True
        settings.qa_cache_ttl_seconds = 600
        ag.reload_from_settings()

        hits = [{"doc_id": 1, "doc_name": "A.pdf", "text": "内容", "score": 0.9, "page": 1}]
        calls = {"n": 0}

        async def fake_retrieve(question, kb_ids=None, top_k=None):
            calls["n"] += 1
            return [dict(h) for h in hits]

        async def fake_stream(question, citations):
            # 模拟模型吐出两段回答
            yield ("answer", {"text": "第一段"})
            yield ("answer", {"text": "第二段"})

        with unittest.mock.patch.object(rag_mod, "retrieve", fake_retrieve), \
             unittest.mock.patch.object(rag_mod, "_stream_mock_answer", fake_stream), \
             unittest.mock.patch.object(settings, "llm_mock", True):
            first = self._collect(rag_mod.stream_answer("公司的地址在哪", [], [1], cache_scope="org:1"))
            second = self._collect(rag_mod.stream_answer("公司的地址在哪", [], [1], cache_scope="org:1"))

        self.assertEqual(calls["n"], 1, "第二次命中缓存却仍然去检索了（缓存没接上）")
        ans2 = "".join(d.get("text", "") for e, d in second if e == "answer")
        self.assertEqual(ans2, "第一段第二段", "缓存返回的答案和原始回答不一致")

    def test_no_scope_means_no_cache(self):
        """调用方没给隔离键时**不使用缓存**（宁可多花钱，也不能串答案）。"""
        from app.services import rag as rag_mod

        settings.qa_cache_enabled = True
        ag.reload_from_settings()

        calls = {"n": 0}

        async def fake_retrieve(question, kb_ids=None, top_k=None):
            calls["n"] += 1
            return [{"doc_id": 1, "doc_name": "A.pdf", "text": "内容", "score": 0.9}]

        async def fake_stream(question, citations):
            yield ("answer", {"text": "答案"})

        with unittest.mock.patch.object(rag_mod, "retrieve", fake_retrieve), \
             unittest.mock.patch.object(rag_mod, "_stream_mock_answer", fake_stream), \
             unittest.mock.patch.object(settings, "llm_mock", True):
            self._collect(rag_mod.stream_answer("问题", [], [1]))
            self._collect(rag_mod.stream_answer("问题", [], [1]))

        self.assertEqual(calls["n"], 2, "没传隔离键却用了缓存 —— 有串客户数据的风险")

    def test_multi_turn_never_uses_cache(self):
        """带历史的多轮追问不能走缓存（"那它呢？"的含义完全取决于上下文）。"""
        from app.services import rag as rag_mod

        settings.qa_cache_enabled = True
        ag.reload_from_settings()

        settings.qa_cache_enabled = True
        calls = {"n": 0}

        async def fake_retrieve(question, kb_ids=None, top_k=None):
            calls["n"] += 1
            return [{"doc_id": 1, "doc_name": "A.pdf", "text": "内容", "score": 0.9}]

        async def fake_stream(question, citations):
            yield ("answer", {"text": "答案"})

        hist = [{"role": "user", "content": "前一句"}, {"role": "assistant", "content": "前一句回答"}]
        with unittest.mock.patch.object(rag_mod, "retrieve", fake_retrieve), \
             unittest.mock.patch.object(rag_mod, "_stream_mock_answer", fake_stream), \
             unittest.mock.patch.object(settings, "llm_mock", True):
            self._collect(rag_mod.stream_answer("公司地址在哪", hist, [1], cache_scope="org:1"))
            self._collect(rag_mod.stream_answer("公司地址在哪", hist, [1], cache_scope="org:1"))

        self.assertEqual(calls["n"], 2, "多轮追问走了缓存，可能答错")


class StreamAnswerBreakerTestCase(unittest.TestCase):
    """熔断接到问答链路上：跳闸时应该**不调用模型**，直接快速失败。"""

    def setUp(self):
        ag.llm_breaker.reset()
        ag.llm_breaker.configure(fail_threshold=2, open_seconds=60, enabled=True)
        self.old_mock = settings.llm_mock

    def tearDown(self):
        ag.llm_breaker.reset()
        settings.llm_mock = self.old_mock

    def test_open_breaker_fails_fast_without_calling_model(self):
        from app.services import rag as rag_mod

        async def fake_retrieve(question, kb_ids=None, top_k=None):
            return [{"doc_id": 1, "doc_name": "A.pdf", "text": "内容", "score": 0.9}]

        called = {"model": 0}

        async def fake_client_call(*a, **kw):
            called["model"] += 1
            raise AssertionError("跳闸了不该再去调模型")

        # 先让它跳闸
        ag.llm_breaker.record_failure()
        ag.llm_breaker.record_failure()
        self.assertEqual(ag.llm_breaker.state, ag.OPEN)

        settings.llm_mock = False
        with unittest.mock.patch.object(rag_mod, "retrieve", fake_retrieve), \
             unittest.mock.patch.object(settings, "llm_api_key", "k"):
            events = _run(_collect(rag_mod.stream_answer("问题", [], [1])))

        errors = [d for e, d in events if e == "error"]
        self.assertTrue(errors, "跳闸时没有快速失败")
        self.assertIn("circuit_open", str(errors[0]))
        self.assertEqual(called["model"], 0)


async def _collect(gen):
    return [evt async for evt in gen]


if __name__ == "__main__":
    unittest.main()
