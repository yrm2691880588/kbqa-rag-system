"""数据清理与归档测试。

这个文件在测什么（大白话）：
    清理脚本是**会删数据**的工具，所以它必须比普通功能更谨慎。
    这里最在意的三件事：

    ① **预览绝不能删数据**。默认那条命令跑一百次，数据也必须一条不少。
       这是全文件最重要的一条 —— 一个"预览"就把数据删了的工具，
       比没有这个工具糟糕得多。

    ② **不该删的坚决不删**。尤其是：
       · 还没到保留期的数据（差一天都不能删）；
       · 用量统计（计费依据）—— 客户半年后要核对账单，得有据可查；
       · 还没过期的验证码（用户可能正等着用它登录）。

    ③ **归档文件要真的能看**。归档的意义就是"删错了还能捞回来"，
       所以文件必须是能读的 JSONL，而且要包含记录下来恢复所需的字段。

运行方式（backend 目录下）：
    python -m unittest tests.test_maintenance -v
"""

import asyncio
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.models.login_failure import LoginFailure
from app.models.message import Message
from app.models.query_log import QueryLog
from app.models.refresh_token import RefreshToken
from app.models.verification_code import VerificationCode
from app.services import maintenance as mt


def _run(coro):
    return asyncio.run(coro)


def _ago(days: float = 0, hours: float = 0) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days, hours=hours)


class MaintenanceTestCase(unittest.TestCase):
    """脚手架：造一批"该删的"和"不该删的"数据。"""

    def setUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="kbqa_archive_"))
        self._old = (settings.log_retention_days, settings.message_retention_days,
                     settings.archive_dir)
        # 归档目录指到临时目录，避免在项目里造出测试文件
        settings.archive_dir = str(self.tmp)

        async def _seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.Session() as db:
                # 99 天前的老日志（该删）+ 89 天前的（不该删，保留期 90 天）
                db.add_all([
                    QueryLog(id=i, user_id=1, question=f"老问题{i}", answered=True,
                             latency_ms=100, created_at=_ago(days=200))
                    for i in range(1, 4)
                ])
                db.add(QueryLog(id=10, user_id=1, question="新一点的问题", answered=True,
                                latency_ms=100, created_at=_ago(days=1)))
                db.add(Message(id=1, session_id=1, role="user", content="很老的消息",
                               created_at=_ago(days=200)))
                db.add(Message(id=2, session_id=1, role="assistant", content="新的消息",
                               created_at=_ago(days=2)))
                # 验证码：一个早就过期（该删）、一个刚过期（宽限期内、不该删）、一个还有效
                db.add(VerificationCode(id=1, kind="sms", target="13800000000",
                                        code_hash="x", expires_at=_ago(days=30),
                                        created_at=_ago(days=31)))
                db.add(VerificationCode(id=2, kind="sms", target="13800000001",
                                        code_hash="x", expires_at=_ago(hours=1),
                                        created_at=_ago(hours=2)))
                db.add(VerificationCode(id=3, kind="sms", target="13800000002",
                                        code_hash="x", expires_at=_ago(days=-1),
                                        created_at=_ago(hours=1)))
                # 令牌：一个已作废很久、一个还有效
                db.add(RefreshToken(id=1, user_id=1, token_hash="t1", revoked=True,
                                    expires_at=_ago(days=30), created_at=_ago(days=40)))
                db.add(RefreshToken(id=2, user_id=1, token_hash="t2", revoked=False,
                                    expires_at=_ago(days=-7), created_at=_ago(days=1)))
                # 登录失败记录：很久以前的（该删）
                db.add(LoginFailure(id=1, username="u", ip="1.1.1.1", created_at=_ago(days=90)))
                await db.commit()

        _run(_seed())

    def tearDown(self):
        settings.log_retention_days, settings.message_retention_days, settings.archive_dir = self._old
        _run(self.engine.dispose())
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _counts(self) -> dict:
        async with self.Session() as db:
            return {
                "query_logs": int(await db.scalar(select(func.count()).select_from(QueryLog)) or 0),
                "messages": int(await db.scalar(select(func.count()).select_from(Message)) or 0),
                "codes": int(await db.scalar(select(func.count()).select_from(VerificationCode)) or 0),
                "tokens": int(await db.scalar(select(func.count()).select_from(RefreshToken)) or 0),
                "failures": int(await db.scalar(select(func.count()).select_from(LoginFailure)) or 0),
            }

    async def _run_cleanup(self, **kw):
        async with self.Session() as db:
            return await mt.run_cleanup(db, **kw)

    # ================================================== ① 预览绝不删数据

    def test_preview_deletes_nothing(self):
        """**全文件最重要的一条**：预览模式一条数据都不许删。

        为什么反复强调：这是"会删数据"的工具，用户第一次运行时一定是想先看看。
        如果预览就删了，用户会措手不及 —— 而且这是不可逆的。
        """
        before = _run(self._counts())
        report = _run(self._run_cleanup(apply=False))
        after = _run(self._counts())

        self.assertFalse(report.applied)
        self.assertEqual(before, after, "预览模式居然把数据删了！")
        self.assertEqual(report.total_deleted, 0)
        self.assertGreater(report.total_planned, 0, "预览应该报告「有多少条符合条件」")
        self.assertEqual(report.archived_files, [], "预览不该产生归档文件")

    def test_preview_reports_counts_per_category(self):
        """预览要分类报告条数，用户才能判断"这个数量合不合理"。"""
        report = _run(self._run_cleanup(apply=False))
        by_name = {p.name: p.count for p in report.plans}
        self.assertEqual(by_name["问答日志"], 3)
        self.assertEqual(by_name["聊天消息"], 1)
        self.assertEqual(by_name["过期验证码"], 1)
        self.assertEqual(by_name["失效登录令牌"], 1)
        self.assertEqual(by_name["登录失败记录"], 1)
        # 用量统计只报告不删
        self.assertIn("用量统计（保留）", by_name)
        self.assertEqual(by_name["用量统计（保留）"], 0)

    # ================================================== ② 不该删的坚决不删

    def test_keeps_data_within_retention_window(self):
        """保留期内的数据一条都不能少（差一天都不行）。"""
        _run(self._run_cleanup(apply=True))
        async def t():
            async with self.Session() as db:
                q = await db.get(QueryLog, 10)      # 1 天前
                m = await db.get(Message, 2)        # 2 天前
                return q, m
        q, m = _run(t())
        self.assertIsNotNone(q, "保留期内的问答日志被删了")
        self.assertIsNotNone(m, "保留期内的聊天消息被删了")

    def test_keeps_unexpired_verification_codes(self):
        """还没过期的验证码不能删 —— 用户可能正等着用它登录。"""
        _run(self._run_cleanup(apply=True))
        async def t():
            async with self.Session() as db:
                return await db.get(VerificationCode, 3)
        self.assertIsNotNone(_run(t()), "还有效的验证码被删了，用户会登不进来")

    def test_keeps_recently_expired_codes_during_grace(self):
        """刚过期的验证码要留宽限期（有人来问"为什么失效"时还能查）。"""
        _run(self._run_cleanup(apply=True))
        async def t():
            async with self.Session() as db:
                return await db.get(VerificationCode, 2)   # 1 小时前过期
        self.assertIsNotNone(_run(t()), "刚过期 1 小时的验证码被删了，没留宽限期")

    def test_keeps_valid_refresh_tokens(self):
        """还有效的登录令牌不能删（删了用户会被强制退出登录）。"""
        _run(self._run_cleanup(apply=True))
        async def t():
            async with self.Session() as db:
                return await db.get(RefreshToken, 2)
        self.assertIsNotNone(_run(t()), "还有效的登录令牌被删了，用户会被强制登出")

    def test_usage_daily_is_never_deleted(self):
        """用量统计是计费依据，**任何情况下都不删**。

        为什么单钉一条：客户半年后说"上个月账单不对"，你得能查出来。
        如果清理脚本把它当"老数据"删了，账就永远说不清了。
        这条测试的做法是：把保留期设成 0 天（最激进），统计依然不能在删除清单里。
        """
        settings.log_retention_days = 0
        report = _run(self._run_cleanup(apply=True))
        self.assertNotIn("用量统计（保留）", report.deleted,
                         "用量统计被删了 —— 计费依据没了，账单再也说不清")
        row = [p for p in report.plans if "用量统计" in p.name][0]
        self.assertIsNone(row.cutoff, "用量统计不该有「删除早于」这个时间点")

    # ================================================== ③ 真删 + 归档

    def test_apply_deletes_only_expired(self):
        """真删：该删的删掉，不该动的原样都在。"""
        before = _run(self._counts())
        report = _run(self._run_cleanup(apply=True))
        after = _run(self._counts())

        self.assertTrue(report.applied)
        self.assertEqual(report.deleted["问答日志"], 3)
        self.assertEqual(report.deleted["聊天消息"], 1)
        self.assertEqual(report.deleted["过期验证码"], 1)
        self.assertEqual(report.deleted["失效登录令牌"], 1)
        self.assertEqual(report.deleted["登录失败记录"], 1)

        self.assertEqual(after["query_logs"], before["query_logs"] - 3)
        self.assertEqual(after["messages"], before["messages"] - 1)
        self.assertEqual(after["codes"], 2, "有效期内的验证码被误删了")
        self.assertEqual(after["tokens"], 1, "有效令牌被误删了")
        self.assertEqual(after["failures"], 0)

    def test_archive_files_are_readable_and_complete(self):
        """归档文件必须能读、且字段够还原数据。

        归档的意义就是"删错了还能捞回来"。如果文件写出来的是一堆乱码、
        或者没记下关键字段（比如问题和时间），那这个归档等于没有。
        """
        report = _run(self._run_cleanup(apply=True))
        self.assertTrue(report.archived_files, "真删了却没生成归档文件")

        ql_files = [f for f in report.archived_files if "问答日志" in f]
        self.assertTrue(ql_files, "问答日志没有归档文件")
        lines = pathlib.Path(ql_files[0]).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 3, "归档条数与被删条数不一致")

        first = json.loads(lines[0])           # 能解析 = 是合法 JSON
        for key in ("id", "question", "created_at", "user_id"):
            self.assertIn(key, first, f"归档里少了 {key}，将来恢复不回来")
        self.assertIn("老问题", first["question"])

    def test_no_archive_option_skips_files(self):
        """明确关掉归档时就别生成文件（但不能因此不删）。"""
        report = _run(self._run_cleanup(apply=True, archive=False))
        self.assertEqual(report.archived_files, [])
        self.assertEqual(report.deleted["问答日志"], 3)

    def test_cleanup_is_idempotent(self):
        """跑第二遍不该又删一遍（第一次已经删干净了）。"""
        first = _run(self._run_cleanup(apply=True))
        second = _run(self._run_cleanup(apply=True))
        self.assertGreater(first.total_deleted, 0)
        self.assertEqual(second.total_deleted, 0, "重复执行又删了一遍数据")
        self.assertEqual(second.total_planned, 0, "第二次预览不该还有可清理项")

    def test_retention_is_configurable(self):
        """保留期从配置读：调大调小都要真的跟着变。"""
        settings.log_retention_days = 250      # 比最老的日志（200 天）还长
        report = _run(self._run_cleanup(apply=False))
        by_name = {p.name: p.count for p in report.plans}
        self.assertEqual(by_name["问答日志"], 0, "保留期调大后还有东西该删？")

        settings.log_retention_days = 100      # 只留 100 天 → 200 天前的那 3 条该删
        report2 = _run(self._run_cleanup(apply=False))
        by_name2 = {p.name: p.count for p in report2.plans}
        self.assertEqual(by_name2["问答日志"], 3)

    def test_zero_retention_falls_back_instead_of_deleting_everything(self):
        """**关键**：保留期填 0 要当"配置写错"，回退成默认，而不是"一条都不留"。

        为什么这么设计：0 天 = 连今天的记录都删，这几乎不可能是本意，
        多半是环境变量占位符忘了改。此时**保守**（多留）比激进（清空）安全得多 ——
        多留只是占点磁盘，误删是找不回来的。

        这个坑本项目的别处已经踩过两次（`if limit:` 把 0 当"不限"、
        `x or 默认值` 把 0 换掉），所以在会删数据的模块里特别单钉一条。
        """
        settings.log_retention_days = 0
        report = _run(self._run_cleanup(apply=False))
        by_name = {p.name: p.count for p in report.plans}
        # 回退成默认 180 天 → 只有 200 天前那 3 条该删，1 天前那条**必须留着**
        self.assertEqual(by_name["问答日志"], 3,
                         "保留期填 0 时把「能删的全删了」当成了正确行为，太危险")

        settings.log_retention_days = -5
        report2 = _run(self._run_cleanup(apply=False))
        by_name2 = {p.name: p.count for p in report2.plans}
        self.assertEqual(by_name2["问答日志"], 3, "负数保留期也该回退，而不是「全删」")

    def test_retention_days_helper(self):
        """兜底规则的单元验证（正数照用、0/负数/非数字回退）。"""
        old = settings.log_retention_days
        try:
            settings.log_retention_days = 30
            self.assertEqual(
                mt._retention_days(settings.log_retention_days, "LOG_RETENTION_DAYS"), 30)
            for bad in (0, -1, "abc", None):
                settings.log_retention_days = bad
                self.assertEqual(
                    mt._retention_days(settings.log_retention_days, "LOG_RETENTION_DAYS"),
                    mt.DEFAULT_RETENTION_DAYS,
                    f"{bad!r} 应该回退到默认值",
                )
        finally:
            settings.log_retention_days = old

    def test_query_log_cutoff_math(self):
        """保留期的算法：cutoff 应该是"现在往前推 N 天"。"""
        settings.log_retention_days = 10
        now = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(mt.query_log_cutoff(now), dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.timezone.utc))


if __name__ == "__main__":
    unittest.main()
