"""限流器共用基类的测试。

这个文件在测什么（大白话）：
    项目里有两套限流（令牌桶 / 滑动窗口），算法不同但共用一批基础设施。
    抽共用基类的最大风险是"顺手把行为改了"。所以这里专门验证：

      ① 两套限流器都还能正常工作（继承后算法没被破坏）
      ② 基类的 purge_idle 真的能清掉长期不用的 key
         —— 不做这件事，限流状态字典会随"来过多少组织/用户"无限增长，
            长期运行后内存只涨不降，属于几个月后才发现的泄漏
      ③ reset() 能清空状态（测试之间互不污染的前提）
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.rate_limit_base import BaseKeyedLimiter


class PurgeIdleTestCase(unittest.TestCase):
    """基类的清理逻辑：长期不活跃的 key 要被丢掉。"""

    def test_purge_removes_stale_keys(self):
        lim = BaseKeyedLimiter()
        # 用一个可控时钟，模拟"很久以前"的活跃记录
        lim._set_state("old", "x", now=0.0)
        lim._set_state("fresh", "y", now=lim._now())

        removed = lim.purge_idle(ttl=10)
        self.assertEqual(removed, 1, "应该只清掉 1 个过期 key")
        self.assertIsNone(lim._get_state("old"), "过期 key 没被清掉")
        self.assertIsNotNone(lim._get_state("fresh"), "活跃 key 被误清")

    def test_purge_keeps_everything_when_all_fresh(self):
        lim = BaseKeyedLimiter()
        now = lim._now()
        for i in range(5):
            lim._set_state(f"k{i}", i, now=now)
        self.assertEqual(lim.purge_idle(ttl=3600), 0)

    def test_reset_clears_all(self):
        lim = BaseKeyedLimiter()
        for i in range(3):
            lim._set_state(f"k{i}", i)
        self.assertEqual(lim.key_count(), 3)
        lim.reset()
        self.assertEqual(lim.key_count(), 0)


class TokenBucketStillWorksTestCase(unittest.TestCase):
    """继承基类后，令牌桶的行为必须完全不变。"""

    def test_allows_up_to_per_minute_then_blocks(self):
        from app.services.ai_guard import RateLimiter

        lim = RateLimiter(per_minute=3)
        now = 1000.0
        # 同一时刻连取 3 个令牌应该都成功
        self.assertTrue(lim.allow("org1", now=now))
        self.assertTrue(lim.allow("org1", now=now))
        self.assertTrue(lim.allow("org1", now=now))
        # 第 4 个应该被拒
        self.assertFalse(lim.allow("org1", now=now))

    def test_zero_means_unlimited(self):
        from app.services.ai_guard import RateLimiter

        lim = RateLimiter(per_minute=0)
        for _ in range(100):
            self.assertTrue(lim.allow("org1"), "per_minute=0 应表示不限流")

    def test_buckets_are_isolated_per_key(self):
        from app.services.ai_guard import RateLimiter

        lim = RateLimiter(per_minute=1)
        now = 1000.0
        self.assertTrue(lim.allow("orgA", now=now))
        self.assertFalse(lim.allow("orgA", now=now), "orgA 应该已被限流")
        self.assertTrue(lim.allow("orgB", now=now), "orgB 不该受 orgA 影响")


class SlidingWindowStillWorksTestCase(unittest.TestCase):
    """继承基类后，滑动窗口的行为必须完全不变。"""

    def test_allows_up_to_limit_then_returns_wait(self):
        from app.api.v1.health import _SlidingWindowLimiter

        lim = _SlidingWindowLimiter(max_calls=2, window_seconds=60)
        self.assertEqual(lim.hit("u1"), 0.0)
        self.assertEqual(lim.hit("u1"), 0.0)
        self.assertGreater(lim.hit("u1"), 0.0, "第 3 次应被拦并返回等待时间")

    def test_keys_isolated(self):
        from app.api.v1.health import _SlidingWindowLimiter

        lim = _SlidingWindowLimiter(max_calls=1, window_seconds=60)
        lim.hit("a")
        self.assertGreater(lim.hit("a"), 0.0)
        self.assertEqual(lim.hit("b"), 0.0, "b 不该受 a 影响")


if __name__ == "__main__":
    unittest.main()
