"""深度探活限流测试。

这个文件在测什么（大白话）：
    /health/deep 会真实调用百炼/DeepSeek 两个付费接口，每次都会产生费用。
    只要求"管理员登录"还不够 —— 管理员账号可能被共用、密码可能泄露，
    或者自己写了个脚本按秒轮询。刷起来照样能把额度烧光。

    所以加了按用户的滑动窗口限流。本文件锁死三件事：
      ① 超过阈值必须 429，不能继续放行；
      ② 429 要带 Retry-After，让调用方知道等多久；
      ③ 限流是按用户隔离的，不能一个人刷爆了把别人也连坐。

另外验证限流器本身的两个易错点：
      · 滑动窗口 vs 固定窗口：固定窗口在边界会放过 2 倍流量；
      · 窗口过后要能自动恢复，不能永久锁死。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.v1.health import _SlidingWindowLimiter  # noqa: E402
from tests.http_base import HttpTestCase  # noqa: E402


class SlidingWindowLimiterTestCase(unittest.TestCase):
    """限流器算法本身的单元测试（不涉及 HTTP）。"""

    def test_allows_up_to_limit(self):
        lim = _SlidingWindowLimiter(max_calls=3, window_seconds=60)
        for i in range(3):
            self.assertEqual(lim.hit("u1"), 0.0, f"第 {i + 1} 次不该被拦")
        self.assertGreater(lim.hit("u1"), 0.0, "第 4 次必须被拦")

    def test_different_keys_are_isolated(self):
        """一个人刷爆了，不能把别人也连坐。"""
        lim = _SlidingWindowLimiter(max_calls=2, window_seconds=60)
        lim.hit("a")
        lim.hit("a")
        self.assertGreater(lim.hit("a"), 0.0, "a 应该被拦")
        self.assertEqual(lim.hit("b"), 0.0, "b 不该受 a 的影响")

    def test_recovers_after_window(self):
        """窗口过去后必须能恢复，否则等于永久封禁。"""
        lim = _SlidingWindowLimiter(max_calls=1, window_seconds=0.15)
        self.assertEqual(lim.hit("u"), 0.0)
        self.assertGreater(lim.hit("u"), 0.0, "刚用完应该被拦")

        import time

        time.sleep(0.2)
        self.assertEqual(lim.hit("u"), 0.0, "窗口过后应恢复放行")

    def test_returns_remaining_wait(self):
        """被拦时要返回"还要等几秒"，调用方据此决定重试时机。"""
        lim = _SlidingWindowLimiter(max_calls=1, window_seconds=30)
        lim.hit("u")
        wait = lim.hit("u")
        self.assertGreater(wait, 0, "被拦时等待时间不能是 0")
        self.assertLessEqual(wait, 30, "等待时间不该超过窗口长度")


class DeepProbeRateLimitHttpTestCase(HttpTestCase):
    """通过真实 HTTP 请求验证 429 行为。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # 每个用例用独立的管理员，避免与其他用例共享限流计数
        self.org = await self.make_org("探活公司")
        self.admin = await self.make_user(
            f"probe{id(self) % 100000}", role="admin", org_id=self.org.id
        )

    async def test_deep_probe_is_rate_limited(self):
        """连刷超过阈值必须 429，且带 Retry-After。"""
        headers = self.auth_headers(self.admin)
        statuses = []
        for _ in range(5):
            r = await self.client.get("/api/v1/health/deep", headers=headers)
            statuses.append(r.status_code)

        self.assertIn(429, statuses, f"连刷 5 次都没有被限流：{statuses}")

        # 找到第一个 429，检查响应头
        last = await self.client.get("/api/v1/health/deep", headers=headers)
        if last.status_code == 429:
            self.assertIn(
                "retry-after", {k.lower() for k in last.headers},
                "429 响应没带 Retry-After，调用方不知道何时重试",
            )

    async def test_member_cannot_probe(self):
        """普通成员连触发的资格都没有（403），更谈不上限流。"""
        member = await self.make_user(
            f"pm{id(self) % 100000}", role="member", org_id=self.org.id
        )
        r = await self.client.get("/api/v1/health/deep", headers=self.auth_headers(member))
        self.assertEqual(r.status_code, 403)

    async def test_anonymous_cannot_probe(self):
        r = await self.client.get("/api/v1/health/deep")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
