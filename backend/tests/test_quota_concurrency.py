"""并发额度测试：验证"原子扣减"在真实并发下不会超发。

这个文件在测什么（大白话）：
    额度控制最容易出的问题是"并发超发"——
    用户还剩 1 次额度时，同时点 5 次提问，如果代码是
    "先查还剩几次 → 再扣一次"，5 个请求会同时查到"还剩 1 次"，
    于是 5 个都放行，实际用了 5 次，上限形同虚设。

    上一轮 code review 正是发现了这个问题（quota.py 原来是读-判-写三步）。
    修复办法是把上限判断塞进 UPDATE 的 WHERE 里，让数据库在行锁内
    一次性完成"读当前值 + 比较 + 加一"。

    本文件就是为了锁死这个行为：**并发下实际扣减次数不得超过上限**。

两种运行模式：
    · SQLite（默认）：串行验证条件语义（SQLite 内存库不支持真并发写入）；
    · PostgreSQL（设了 KBQA_TEST_PG_URL 时）：用 asyncio.gather 发起真并发，
      这才是真正能暴露竞态的测试。

运行方式（backend 目录下）：
    # 只跑 SQLite 部分
    python -m unittest tests.test_quota_concurrency -v

    # 连同 PostgreSQL 真并发一起跑
    set KBQA_TEST_PG_URL=postgresql+asyncpg://kbqa:pw@127.0.0.1:55432/kbqa_test
    python -m unittest tests.test_quota_concurrency -v
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

PG_URL = os.environ.get("KBQA_TEST_PG_URL", "").strip()


class _Base:
    """公共脚手架：建库 + 建组织 + 设额度。"""

    URL = "sqlite+aiosqlite://"
    ENGINE_KW = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}

    async def _setup_schema(self):
        self.engine = create_async_engine(self.URL, **self.ENGINE_KW)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def _seed_org(self, org_id: int = 1, daily_limit: int | None = 5):
        async with self.Session() as db:
            db.add(Organization(id=org_id, name=f"公司{org_id}", invite_code=f"code-{org_id}"))
            await db.flush()
            db.add(OrgQuota(org_id=org_id, daily_question_limit=daily_limit))
            await db.commit()

    async def _clean(self):
        await self.engine.dispose()

    async def _used_questions(self, org_id: int = 1) -> int:
        async with self.Session() as db:
            row = await q.get_daily_usage(db, org_id)
            return row.questions if row else 0


class ConditionalDeductTestCase(_Base, unittest.IsolatedAsyncioTestCase):
    """条件扣减的语义正确性（两种数据库都跑）。"""

    async def asyncSetUp(self):
        await self._setup_schema()

    async def asyncTearDown(self):
        await self._clean()

    async def test_sequential_never_exceeds_limit(self):
        """串行连问 10 次，上限 5：必须只有 5 次成功。"""
        await self._seed_org(daily_limit=5)
        allowed = 0
        for _ in range(10):
            async with self.Session() as db:
                d = await q.check_and_consume_question(db, 1)
                if d.allowed:
                    allowed += 1
        self.assertEqual(allowed, 5, f"上限 5 却放行了 {allowed} 次")
        self.assertEqual(await self._used_questions(), 5, "扣减次数与实际放行次数不符")

    async def test_limit_zero_blocks_everything(self):
        """上限 0 = 一次都不给（不是"不限"）。"""
        await self._seed_org(daily_limit=0)
        async with self.Session() as db:
            d = await q.check_and_consume_question(db, 1)
        self.assertFalse(d.allowed, "上限 0 却放行了")
        self.assertEqual(await self._used_questions(), 0, "被拒绝的请求不该扣用量")

    async def test_none_limit_is_unlimited(self):
        """上限 None = 不限。"""
        await self._seed_org(daily_limit=None)
        for _ in range(20):
            async with self.Session() as db:
                d = await q.check_and_consume_question(db, 1)
                self.assertTrue(d.allowed, "不限额度却被拦住了")
        self.assertEqual(await self._used_questions(), 20)

    async def test_rejected_request_does_not_bump(self):
        """被拒的请求不能留下用量记录（否则客户会看到凭空多出来的次数）。"""
        await self._seed_org(daily_limit=1)
        async with self.Session() as db:
            await q.check_and_consume_question(db, 1)
        before = await self._used_questions()
        async with self.Session() as db:
            d = await q.check_and_consume_question(db, 1)
            self.assertFalse(d.allowed)
        self.assertEqual(await self._used_questions(), before, "被拒的请求也扣了用量")

    async def test_try_consume_returns_false_when_exhausted(self):
        """底层原子函数在额度用尽时必须返回 False，而不是静默加一。"""
        await self._seed_org(daily_limit=2)
        day = q.today_local()
        async with self.Session() as db:
            self.assertTrue(await q._try_consume_question(db, 1, day, d_limit=2, m_limit=None))
            self.assertTrue(await q._try_consume_question(db, 1, day, d_limit=2, m_limit=None))
            self.assertFalse(
                await q._try_consume_question(db, 1, day, d_limit=2, m_limit=None),
                "额度用尽后仍返回 True（会无限超发）",
            )
        self.assertEqual(await self._used_questions(), 2)

    async def test_monthly_limit_enforced_on_first_question_of_day(self):
        """新的一天第一次提问时，月上限仍然生效（不能因为"今天还没记录"就绕开）。"""
        # 建组织时就把"日不限、月上限 3"一次设好，避免重复插入 OrgQuota 触发唯一约束
        async with self.Session() as db:
            db.add(Organization(id=1, name="公司1", invite_code="code-1"))
            await db.flush()
            db.add(
                OrgQuota(
                    org_id=1,
                    daily_question_limit=None,
                    monthly_question_limit=3,
                )
            )
            today = q.today_local()
            db.add(UsageDaily(org_id=1, day=today, questions=3))
            await db.commit()
        async with self.Session() as db:
            d = await q.check_and_consume_question(db, 1)
        self.assertFalse(d.allowed, "月额度已满却放行了")
        self.assertEqual(d.status_code, 402)


@unittest.skipUnless(PG_URL, "未设置 KBQA_TEST_PG_URL，跳过真实 PostgreSQL 并发测试")
class PostgresConcurrencyTestCase(_Base, unittest.IsolatedAsyncioTestCase):
    """真实并发测试：只有真数据库才能真正暴露竞态。

    为什么必须用 PostgreSQL 跑：
        SQLite 内存库是单连接（StaticPool），并发请求实际是排队执行的，
        永远测不出竞态。而 PostgreSQL 支持多连接真并发，
        如果扣减逻辑不是原子的，这里就会看到"放行次数 > 上限"。
    """

    URL = PG_URL
    ENGINE_KW = {"pool_size": 10, "max_overflow": 10}

    async def asyncSetUp(self):
        await self._setup_schema()
        async with self.Session() as db:
            # 每轮测试前清空，避免上次残留
            from sqlalchemy import delete

            await db.execute(delete(UsageDaily))
            await db.execute(delete(OrgQuota))
            await db.execute(delete(Organization))
            await db.commit()

    async def asyncTearDown(self):
        await self._clean()

    async def test_concurrent_never_exceeds_limit(self):
        """10 个并发请求抢 3 个额度：必须正好 3 个成功。"""
        limit = 3
        concurrency = 10
        await self._seed_org(daily_limit=limit)

        async def one() -> bool:
            async with self.Session() as db:
                d = await q.check_and_consume_question(db, 1)
                return d.allowed

        results = await asyncio.gather(*[one() for _ in range(concurrency)])
        allowed = sum(1 for r in results if r)
        used = await self._used_questions()

        self.assertEqual(
            allowed, limit,
            f"并发下放行了 {allowed} 次，上限是 {limit}（超发说明扣减不是原子的）",
        )
        self.assertEqual(
            used, limit,
            f"实际扣减 {used} 次，与放行次数 {allowed} 不一致（账目错乱）",
        )

    async def test_concurrent_usage_never_exceeds_capacity(self):
        """更严格：并发 30 次、上限 10，最终用量必须恰好 10。"""
        limit = 10
        await self._seed_org(daily_limit=limit)

        async def one():
            async with self.Session() as db:
                await q.check_and_consume_question(db, 1)

        await asyncio.gather(*[one() for _ in range(30)])
        used = await self._used_questions()
        self.assertEqual(used, limit, f"并发 30 次后用量是 {used}，应恰好为 {limit}")


if __name__ == "__main__":
    unittest.main()
