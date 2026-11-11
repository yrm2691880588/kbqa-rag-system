"""用户反馈功能的测试。

这个文件在测什么（大白话）：
    用户点了"踩"之后要能说清"哪里不准确"，管理员要能看到这些反馈
    并据此改进知识库。这条链路上有几个必须钉死的点：

      ① 原因必须是预定义的那几个 —— 否则数据库里会出现统计不到的脏值
      ② 点赞时不该留下原因（避免"满意 + 抱怨原因"的矛盾记录）
      ③ 取消评价要把原因和说明一起清掉（不能只清 feedback）
      ④ 跨组织看不到别人的反馈（消息表没有 org_id，要沿 session→user 收敛）
      ⑤ 统计的满意率：没有评价时是 None，不是 0
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from app.db.base import Base
from app.models.message import Message
from app.models.organization import Organization
from app.models.session import ChatSession
from app.models.user import User
from app.core.security import hash_password
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


def _run(coro):
    return asyncio.run(coro)


class FeedbackBaseTestCase(unittest.TestCase):
    """公共脚手架：两个组织，各一个管理员 + 一条被评价的回答。"""

    def setUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        _run(self._seed())

    def tearDown(self):
        _run(self.engine.dispose())

    async def _seed(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.Session() as db:
            db.add(Organization(id=1, name="甲公司", invite_code="c1"))
            db.add(Organization(id=2, name="乙公司", invite_code="c2"))
            await db.flush()
            u1 = User(username="adminA", password_hash=hash_password("Pass1234!"), role="admin", org_id=1)
            u2 = User(username="adminB", password_hash=hash_password("Pass1234!"), role="admin", org_id=2)
            db.add_all([u1, u2])
            await db.flush()
            s1 = ChatSession(user_id=u1.id, title="甲的问题", kb_scope=None)
            s2 = ChatSession(user_id=u2.id, title="乙的问题", kb_scope=None)
            db.add_all([s1, s2])
            await db.flush()
            m1 = Message(session_id=s1.id, role="assistant", content="甲的答案", feedback=-1,
                         feedback_reason="inaccurate", feedback_note="错了")
            m2 = Message(session_id=s2.id, role="assistant", content="乙的答案", feedback=-1,
                         feedback_reason="irrelevant")
            db.add_all([m1, m2])
            await db.commit()
            await db.refresh(s1)
            await db.refresh(s2)
            await db.refresh(m1)
            await db.refresh(m2)
            self.s1_id, self.s2_id = s1.id, s2.id
            self.m1_id, self.m2_id = m1.id, m2.id
            self.u1_id, self.u2_id = u1.id, u2.id


class FeedbackSchemaValidationTestCase(unittest.TestCase):
    """原因必须是预定义的 key。"""

    def test_valid_reason_accepted(self):
        from app.schemas.chat import FeedbackRequest

        req = FeedbackRequest(feedback=-1, reason="inaccurate")
        self.assertEqual(req.reason, "inaccurate")

    def test_invalid_reason_rejected(self):
        from app.schemas.chat import FeedbackRequest

        with self.assertRaises(Exception):
            FeedbackRequest(feedback=-1, reason="not-a-real-reason")

    def test_empty_reason_becomes_none(self):
        from app.schemas.chat import FeedbackRequest

        req = FeedbackRequest(feedback=-1, reason="")
        self.assertIsNone(req.reason)

    def test_all_preset_reasons_are_valid(self):
        """每个预定义原因都必须能通过校验（防止加了分类却忘记同步）。"""
        from app.schemas.chat import FEEDBACK_REASONS, FeedbackRequest

        for key in FEEDBACK_REASONS:
            req = FeedbackRequest(feedback=-1, reason=key)
            self.assertEqual(req.reason, key)


class FeedbackPersistTestCase(FeedbackBaseTestCase):
    """落库行为：点赞清原因、取消全清、踩才存原因。"""

    def test_dislike_saves_reason_and_note(self):
        async def t():
            from app.api.v1.sessions import set_message_feedback
            from app.schemas.chat import FeedbackRequest

            async with self.Session() as db:
                admin = await db.get(User, self.u1_id)
                await set_message_feedback(
                    self.s1_id, self.m1_id,
                    FeedbackRequest(feedback=-1, reason="incomplete", note="没说充电时间"),
                    admin, db,
                )
            async with self.Session() as db:
                m = await db.get(Message, self.m1_id)
                self.assertEqual(m.feedback, -1)
                self.assertEqual(m.feedback_reason, "incomplete")
                self.assertEqual(m.feedback_note, "没说充电时间")
                self.assertIsNotNone(m.feedback_at, "反馈时间没记")
        _run(t())

    def test_like_clears_reason(self):
        """点赞不该保留原因 —— 否则会出现"满意 + 抱怨原因"的矛盾记录。"""
        async def t():
            from app.api.v1.sessions import set_message_feedback
            from app.schemas.chat import FeedbackRequest

            async with self.Session() as db:
                admin = await db.get(User, self.u1_id)
                await set_message_feedback(
                    self.s1_id, self.m1_id,
                    FeedbackRequest(feedback=1),
                    admin, db,
                )
            async with self.Session() as db:
                m = await db.get(Message, self.m1_id)
                self.assertEqual(m.feedback, 1)
                self.assertIsNone(m.feedback_reason, "点赞后原因没清掉")
                self.assertIsNone(m.feedback_note)
        _run(t())

    def test_cancel_clears_everything(self):
        """取消评价要把 feedback / reason / note / at 全部清空。"""
        async def t():
            from app.api.v1.sessions import set_message_feedback
            from app.schemas.chat import FeedbackRequest

            async with self.Session() as db:
                admin = await db.get(User, self.u1_id)
                await set_message_feedback(
                    self.s1_id, self.m1_id, FeedbackRequest(feedback=0), admin, db
                )
            async with self.Session() as db:
                m = await db.get(Message, self.m1_id)
                self.assertIsNone(m.feedback)
                self.assertIsNone(m.feedback_reason)
                self.assertIsNone(m.feedback_note)
                self.assertIsNone(m.feedback_at)
        _run(t())

    def test_cannot_feedback_other_org_message(self):
        """跨组织改别人的反馈必须 404。"""
        async def t():
            from app.api.v1.sessions import set_message_feedback
            from app.schemas.chat import FeedbackRequest

            async with self.Session() as db:
                adminA = await db.get(User, self.u1_id)
                with self.assertRaises(HTTPException) as ctx:
                    await set_message_feedback(
                        self.s2_id, self.m2_id, FeedbackRequest(feedback=1), adminA, db
                    )
                self.assertEqual(ctx.exception.status_code, 404)
        _run(t())


class FeedbackQueryScopeTestCase(FeedbackBaseTestCase):
    """管理端查询：按组织收敛 + 统计口径正确。"""

    def test_list_only_returns_own_org(self):
        async def t():
            from app.api.v1.feedbacks import list_feedbacks

            async with self.Session() as db:
                adminA = await db.get(User, self.u1_id)
                rows = await list_feedbacks(adminA, db, feedback=None, reason=None,
                                            limit=50, offset=0)
            ids = [r.message_id for r in rows]
            self.assertIn(self.m1_id, ids)
            self.assertNotIn(self.m2_id, ids, "看到了别的组织的反馈")
        _run(t())

    def test_stats_scoped_and_rate_correct(self):
        async def t():
            from app.api.v1.feedbacks import feedback_stats

            async with self.Session() as db:
                adminA = await db.get(User, self.u1_id)
                s = await feedback_stats(adminA, db)
            # 甲组织只有 1 条踩
            self.assertEqual(s.total, 1)
            self.assertEqual(s.dislikes, 1)
            self.assertEqual(s.likes, 0)
            self.assertEqual(s.satisfaction_rate, 0.0)
            # 原因分布里应该有 inaccurate
            reasons = {r["reason"] for r in s.reason_counts}
            self.assertIn("inaccurate", reasons)
        _run(t())

    def test_detail_of_other_org_is_404(self):
        async def t():
            from app.api.v1.feedbacks import feedback_detail

            async with self.Session() as db:
                adminA = await db.get(User, self.u1_id)
                with self.assertRaises(HTTPException) as ctx:
                    await feedback_detail(self.m2_id, adminA, db)
                self.assertEqual(ctx.exception.status_code, 404)
        _run(t())

    def test_rate_is_none_when_no_feedback(self):
        """没有任何评价时满意率应为 None（不是 0）。

        0 会被理解为"有评价但全都不满意"，与"还没人评价"完全不同。
        """
        async def t():
            from app.api.v1.feedbacks import feedback_stats
            from sqlalchemy import update

            async with self.Session() as db:
                # 把所有反馈清掉
                await db.execute(update(Message).values(feedback=None, feedback_reason=None))
                await db.commit()

                adminA = await db.get(User, self.u1_id)
                s = await feedback_stats(adminA, db)
            self.assertEqual(s.total, 0)
            self.assertIsNone(s.satisfaction_rate, "没有评价时满意率应为 None")
        _run(t())


if __name__ == "__main__":
    unittest.main()
