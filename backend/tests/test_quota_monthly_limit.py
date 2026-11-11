"""月额度不可被突破的回归测试。

这个文件在测什么（大白话）：
    额度控制是"按用量收钱"的前提。如果客户能突破月上限，
    要么平台亏钱、要么账单和实际用量对不上。

    早期实现有个真实漏洞：_try_consume_question 的原子 UPDATE 里
    只带了"日上限"条件，月上限只在"当天还没有用量行"时检查一次。
    于是当天一旦有了用量行，后续扣减就完全忽略月上限 ——
    并发下能无限突破（实测：上限 3，并发 10 次，累计跑到 8）。

    本文件把"月上限必须在扣减路径上生效"这件事钉死。
"""

import asyncio
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.org_quota import OrgQuota
from app.models.organization import Organization
from app.models.usage_daily import UsageDaily
from app.services import quota as q


class MonthlyLimitOnConsumePathTestCase(unittest.IsolatedAsyncioTestCase):
    """月上限必须在原子扣减路径上生效（不能只在"当天无用量行"时检查）。"""

    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.today = q.today_local()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed(self, *, month_limit, past_days_usage=0, today_row=True):
        async with self.Session() as db:
            db.add(Organization(id=1, name="测试公司", invite_code="c1"))
            await db.flush()
            db.add(
                OrgQuota(
                    org_id=1,
                    daily_question_limit=None,
                    monthly_question_limit=month_limit,
                )
            )
            if past_days_usage:
                db.add(
                    UsageDaily(
                        org_id=1,
                        day=self.today - dt.timedelta(days=3),
                        questions=past_days_usage,
                    )
                )
            if today_row:
                # 关键前提：当天**已经有**用量行。
                # 这正是旧实现漏检月上限的场景。
                db.add(UsageDaily(org_id=1, day=self.today, questions=0))
            await db.commit()

    async def test_month_limit_blocks_when_today_row_exists(self):
        """当天已有用量行时，月上限仍然生效（旧实现会漏掉这里）。"""
        await self._seed(month_limit=3, past_days_usage=3, today_row=True)
        async with self.Session() as db:
            ok = await q._try_consume_question(
                db, 1, self.today, d_limit=None, m_limit=3
            )
        self.assertFalse(ok, "月额度已满却扣减成功（月上限没有在扣减路径上生效）")

    async def test_month_usage_never_exceeds_limit_over_many_calls(self):
        """连续扣减多次，累计月用量不得超过上限。"""
        await self._seed(month_limit=3, past_days_usage=0, today_row=True)
        allowed = 0
        for _ in range(10):
            async with self.Session() as db:
                if await q._try_consume_question(db, 1, self.today, d_limit=None, m_limit=3):
                    allowed += 1
        async with self.Session() as db:
            month = await q.get_month_usage(db, 1)
        self.assertEqual(allowed, 3, f"上限 3 却放行了 {allowed} 次")
        self.assertLessEqual(
            month["questions"], 3,
            f"月用量突破上限：{month['questions']} > 3",
        )

    async def test_allows_when_under_limit(self):
        """没超限时必须正常放行（别把功能拦死）。"""
        await self._seed(month_limit=100, today_row=True)
        ok_count = 0
        for _ in range(5):
            async with self.Session() as db:
                if await q._try_consume_question(db, 1, self.today, d_limit=None, m_limit=100):
                    ok_count += 1
        self.assertEqual(ok_count, 5, "额度充足时被误拦")

    async def test_none_month_limit_is_unlimited(self):
        """月上限 None = 不限，必须始终放行。"""
        await self._seed(month_limit=None, today_row=True)
        ok_count = 0
        for _ in range(10):
            async with self.Session() as db:
                if await q._try_consume_question(db, 1, self.today, d_limit=None, m_limit=None):
                    ok_count += 1
        self.assertEqual(ok_count, 10, "不限月额度时被拦住")

    async def test_day_limit_still_works(self):
        """修月上限不能把日上限搞坏。"""
        await self._seed(month_limit=None, today_row=True)
        ok_count = 0
        for _ in range(10):
            async with self.Session() as db:
                # 日上限 2，月不限
                if await q._try_consume_question(db, 1, self.today, d_limit=2, m_limit=None):
                    ok_count += 1
        self.assertEqual(ok_count, 2, f"日上限 2 却放行了 {ok_count} 次")

    async def test_both_limits_enforced(self):
        """日/月同时设置时，以先到者为准。"""
        await self._seed(month_limit=5, today_row=True)
        allowed = 0
        for _ in range(10):
            async with self.Session() as db:
                # 日上限 2 更严格
                if await q._try_consume_question(db, 1, self.today, d_limit=2, m_limit=5):
                    allowed += 1
        self.assertEqual(allowed, 2, f"日/月双限制下放行了 {allowed} 次，应为 2")


if __name__ == "__main__":
    unittest.main()
