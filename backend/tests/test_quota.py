"""额度与用量测试（P1-B）。

这个文件在测什么（大白话）：
    给每个客户组织设一个"每天最多问多少次 / 每月最多问多少次"的上限，
    防止账号被盗或者有人恶意刷的时候，大模型账单一直涨。

    几件必须较真的事：
      ① **"不限"和"零次"不能混**：没写上限 = 不限；写了 0 = 一次都不给。
         这两个在代码里分别对应 None 和 0，最容易写错（`if limit:` 会把 0 当成"不限"，
         结果"停用"变成了"随便用"——正好反了）。
      ② **到顶之后不能再扣**：如果超了还继续给用量加数字，用量会一直涨，
         以后客户说"我只问了 100 次怎么显示 300 次"，账就对不上了。
      ③ **被停服的组织，哪怕额度没用完也不能用**。
      ④ **平台超管不受限**（你自己用不该被自己的额度卡住）。

运行方式（backend 目录下）：
    python -m unittest tests.test_quota -v
"""

import asyncio
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.org_quota import OrgQuota
from app.models.organization import Organization
from app.models.usage_daily import UsageDaily
from app.models.user import User
from app.services import quota as q


def _run(coro):
    return asyncio.run(coro)


class QuotaTestCase(unittest.TestCase):
    """公共脚手架：两家客户组织 + 一个平台超管。"""

    def setUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        _run(self._seed())

    async def _seed(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.Session() as db:
            db.add_all([
                Organization(id=1, name="A公司", invite_code="code-a"),
                Organization(id=2, name="B公司", invite_code="code-b"),
            ])
            await db.flush()
            db.add_all([
                User(id=1, username="admin_a", password_hash="x", role="admin", org_id=1),
                User(id=2, username="admin_b", password_hash="x", role="admin", org_id=2),
                User(id=9, username="root", password_hash="x", role="admin",
                     org_id=None, is_superadmin=True),
            ])
            await db.commit()

    async def _set_quota(self, org_id: int, **kwargs):
        async with self.Session() as db:
            db.add(OrgQuota(org_id=org_id, **kwargs))
            await db.commit()

    async def _ask(self, org_id, times: int = 1):
        """模拟连续提问 times 次，返回每次都判定结果。"""
        out = []
        async with self.Session() as db:
            for _ in range(times):
                out.append(await q.check_and_consume_question(db, org_id))
        return out

    async def _usage(self, org_id, day=None):
        async with self.Session() as db:
            return await q.get_daily_usage(db, org_id, day)

    # ================================================== 不限 / 零次

    def test_no_quota_row_means_unlimited(self):
        """没配过额度 = 不限：老客户升级后不能被凭空拦住。"""
        async def t():
            decisions = await self._ask(1, 5)
            self.assertTrue(all(d.allowed for d in decisions))
            u = await self._usage(1)
            self.assertEqual(u.questions, 5, "用量没记上")
        _run(t())

    def test_zero_limit_blocks_everything(self):
        """**关键**：上限写 0 = 一次都不给。

        这一条是防"把 0 当成不限"的经典写法错误（`if limit:` 判断）；
        真写错了，"暂停某个客户"就会变成"这个客户随便用"。
        """
        async def t():
            await self._set_quota(1, daily_question_limit=0)
            decisions = await self._ask(1, 2)
            self.assertFalse(decisions[0].allowed, "上限为 0 却放行了")
            self.assertEqual(decisions[0].status_code, 429)
            # 被拒绝的请求不应该产生用量
            u = await self._usage(1)
            self.assertTrue(u is None or u.questions == 0, "被拒绝的请求也记了用量")
        _run(t())

    def test_none_limit_is_unlimited(self):
        """上限写 None = 不限（和 0 是相反的意思）。"""
        async def t():
            await self._set_quota(1, daily_question_limit=None, monthly_question_limit=None)
            decisions = await self._ask(1, 3)
            self.assertTrue(all(d.allowed for d in decisions))
        _run(t())

    # ================================================== 到顶

    def test_daily_limit_blocks_at_boundary(self):
        """日上限 3：前 3 次放行，第 4 次拒绝。边界要准。"""
        async def t():
            await self._set_quota(1, daily_question_limit=3)
            decisions = await self._ask(1, 4)
            self.assertEqual([d.allowed for d in decisions], [True, True, True, False])
            u = await self._usage(1)
            self.assertEqual(u.questions, 3, "到顶后不该再累加用量")
        _run(t())

    def test_rejected_request_does_not_bump_usage(self):
        """被拒之后用量必须停住（否则账目会一直涨）。"""
        async def t():
            await self._set_quota(1, daily_question_limit=2)
            await self._ask(1, 2)
            before = (await self._usage(1)).questions
            await self._ask(1, 5)      # 全部会被拒
            after = (await self._usage(1)).questions
            self.assertEqual(before, after, "被拒绝的请求仍然在累加用量")
        _run(t())

    def test_monthly_limit_returns_402(self):
        """月额度用完 → 402（付费类状态码，区别于"今天太频繁"的 429）。"""
        async def t():
            await self._set_quota(1, monthly_question_limit=2)
            decisions = await self._ask(1, 3)
            self.assertEqual([d.allowed for d in decisions], [True, True, False])
            self.assertEqual(decisions[2].status_code, 402)
        _run(t())

    def test_month_limit_counts_across_days(self):
        """月额度要跨天累计：昨天用的也算在本月里。"""
        async def t():
            await self._set_quota(1, monthly_question_limit=5)
            async with self.Session() as db:
                db.add(UsageDaily(org_id=1, day=q.today_local() - dt.timedelta(days=1), questions=4))
                await db.commit()
            decisions = await self._ask(1, 2)
            self.assertTrue(decisions[0].allowed, "昨天用了 4 次，本月还剩 1 次，应该放行")
            self.assertFalse(decisions[1].allowed, "月额度已到顶，第二次应该被拒")
        _run(t())

    def test_daily_resets_next_day(self):
        """日额度是滚动的：昨天用满了，今天应该恢复。"""
        async def t():
            await self._set_quota(1, daily_question_limit=2)
            async with self.Session() as db:
                db.add(UsageDaily(org_id=1, day=q.today_local() - dt.timedelta(days=1), questions=2))
                await db.commit()
            decisions = await self._ask(1, 1)
            self.assertTrue(decisions[0].allowed, "日额度没有按天重置")
        _run(t())

    # ================================================== 停服

    def test_disabled_service_blocks_even_with_quota_left(self):
        """组织被整体停服 → 哪怕额度一次没用也拒绝（欠费/合同到期场景）。"""
        async def t():
            await self._set_quota(1, daily_question_limit=100, is_service_enabled=False)
            decisions = await self._ask(1, 1)
            self.assertFalse(decisions[0].allowed)
            self.assertEqual(decisions[0].status_code, 402)
            self.assertIn("暂停", decisions[0].reason)
        _run(t())

    # ================================================== 超管

    def test_superadmin_is_never_limited(self):
        """平台超管（不属于任何组织）不受额度限制，也不产生用量。"""
        async def t():
            decisions = await self._ask(None, 5)
            self.assertTrue(all(d.allowed for d in decisions))
            async with self.Session() as db:
                rows = (await db.execute(UsageDaily.__table__.select())).all()
                self.assertEqual(len(rows), 0, "超管的请求不该记进任何组织的用量")
        _run(t())

    # ================================================== 累加正确性

    def test_usage_accumulates_atomically(self):
        """反复调用累加，数字必须精确（不能丢计数）。"""
        async def t():
            async with self.Session() as db:
                for _ in range(7):
                    await q._bump(db, 1, q.today_local(), questions=1, tokens_in=10, tokens_out=20)
            u = await self._usage(1)
            self.assertEqual(u.questions, 7)
            self.assertEqual(u.tokens_in, 70)
            self.assertEqual(u.tokens_out, 140)
            # 同一天只能有一行（唯一约束在起作用）
            async with self.Session() as db:
                rows = (await db.execute(
                    UsageDaily.__table__.select().where(UsageDaily.org_id == 1)
                )).all()
                self.assertEqual(len(rows), 1, "同一天被写了多行，额度会算不准")
        _run(t())

    def test_token_usage_recorded(self):
        """token 用量单独记（和"次数"分开，因为要等回答生成完才知道）。"""
        async def t():
            async with self.Session() as db:
                await q.add_llm_tokens(db, 1, 1200, 300)
            u = await self._usage(1)
            self.assertEqual(u.tokens_in, 1200)
            self.assertEqual(u.tokens_out, 300)
            self.assertEqual(u.questions, 0, "记 token 不该顺带把提问次数加上")
        _run(t())

    def test_token_limit_blocks(self):
        """配了 token 上限时，超了也要拦。"""
        async def t():
            await self._set_quota(1, daily_token_limit=100)
            async with self.Session() as db:
                await q.add_llm_tokens(db, 1, 80, 30)   # 合计 110 > 100
            decisions = await self._ask(1, 1)
            self.assertFalse(decisions[0].allowed)
        _run(t())

    def test_orgs_are_metered_separately(self):
        """A 公司用满了，不能影响 B 公司。"""
        async def t():
            await self._set_quota(1, daily_question_limit=1)
            a = await self._ask(1, 2)
            b = await self._ask(2, 2)
            self.assertEqual([d.allowed for d in a], [True, False])
            self.assertTrue(all(d.allowed for d in b), "A 公司的额度把 B 公司也卡住了")
        _run(t())

    # ================================================== 汇总与权限

    def test_summary_shape(self):
        """汇总接口要能同时给出今日/本月用量与上限（前端就靠它展示）。"""
        async def t():
            await self._set_quota(1, daily_question_limit=10, monthly_question_limit=100)
            await self._ask(1, 3)
            async with self.Session() as db:
                s = await q.get_summary(db, 1)
            self.assertEqual(s["daily"]["questions"], 3)
            self.assertEqual(s["daily"]["question_limit"], 10)
            self.assertEqual(s["monthly"]["questions"], 3)
            self.assertEqual(s["monthly"]["question_limit"], 100)
            self.assertTrue(s["is_service_enabled"])

            # 出口模型要能吃下这份数据（否则接口会 500）
            from app.schemas.quota import UsageSummaryOut
            out = UsageSummaryOut.model_validate(s)
            self.assertEqual(out.org_id, 1)
            self.assertEqual(out.daily.day, q.today_local())
        _run(t())

    def test_cannot_view_other_org_usage(self):
        """组织管理员不能看别家组织的用量，且越权返回 404（不承认对方存在）。"""
        async def t():
            from app.api.v1.quotas import _assert_can_view
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                admin_b = await db.get(User, 2)
                root = await db.get(User, 9)

                await _assert_can_view(admin_a, 1)      # 自己组织：放行
                await _assert_can_view(root, 2)         # 超管：放行

                with self.assertRaises(HTTPException) as ctx:
                    await _assert_can_view(admin_a, 2)  # 看别家：404
                self.assertEqual(ctx.exception.status_code, 404)
                _ = admin_b
        _run(t())

    def test_only_superadmin_can_change_quota(self):
        """额度只有平台超管能改（组织管理员自己调高上限的话，上限就没意义了）。"""
        async def t():
            from app.api.v1.quotas import update_org_quota
            from app.core.deps import require_superadmin
            from app.schemas.quota import QuotaUpdate

            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                with self.assertRaises(HTTPException) as ctx:
                    await require_superadmin(admin_a)
                self.assertEqual(ctx.exception.status_code, 403)

            # 超管改得动
            async with self.Session() as db:
                root = await db.get(User, 9)
                got = await update_org_quota(1, QuotaUpdate(daily_question_limit=50), root, db)
                self.assertEqual(got.daily_question_limit, 50)
        _run(t())

    def test_partial_update_does_not_wipe_other_fields(self):
        """只改备注时，其它额度字段必须保持原样。

        这条防的是一个很讨厌的静默事故：运营只想改一下备注，
        结果把所有额度顺手清空成"不限"（等于给客户开了无限量）。
        """
        async def t():
            from app.api.v1.quotas import update_org_quota
            from app.schemas.quota import QuotaUpdate

            async with self.Session() as db:
                root = await db.get(User, 9)
                await update_org_quota(
                    1, QuotaUpdate(daily_question_limit=30, monthly_question_limit=300), root, db
                )
                # 这次只传备注
                got = await update_org_quota(1, QuotaUpdate(note="2026 年合同"), root, db)
                self.assertEqual(got.daily_question_limit, 30, "只改备注却把日额度清掉了")
                self.assertEqual(got.monthly_question_limit, 300)
                self.assertEqual(got.note, "2026 年合同")

                # 显式传 null 才是"改成不限"
                got2 = await update_org_quota(1, QuotaUpdate(daily_question_limit=None), root, db)
                self.assertIsNone(got2.daily_question_limit)
                self.assertEqual(got2.monthly_question_limit, 300, "改一项影响了另一项")
        _run(t())


if __name__ == "__main__":
    unittest.main()
