"""回答反馈接口测试（P1-E）。

这个文件在测什么（大白话）：
    问答页每条 AI 回答下面有「赞 / 踩」两个按钮。以前只是网页上变个颜色，
    刷新就没了——等于白点。现在要真的存进数据库。
    但"存进去"这件事有个大坑：消息表里没有"归属人"这一列，
    只有 session_id。如果不逐层校验，任何人把网址里的数字改一改，
    就能给别人的回答打标签（既污染统计，也能反推出"这条消息存在"）。

    所以本文件重点测两件事：
      ① 正常的赞 / 踩 / 取消 能不能正确落库；
      ② 越权（给别人打分、给别的会话的消息打分、给自己的提问打分）能不能被挡住。

运行方式（backend 目录下）：
    python -m unittest tests.test_feedback_api -v
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.message import Message
from app.models.organization import Organization
from app.models.session import ChatSession
from app.models.user import User
from app.schemas.chat import FeedbackRequest


def _run(coro):
    return asyncio.run(coro)


class FeedbackApiTestCase(unittest.TestCase):
    """公共脚手架：两个用户、各自一个会话，每个会话里一问一答。"""

    def setUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        _run(self._seed())

    async def _seed(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.Session() as db:
            db.add(Organization(id=1, name="A公司", invite_code="code-a"))
            await db.flush()
            db.add_all([
                User(id=1, username="alice", password_hash="x", role="user", org_id=1),
                User(id=2, username="bob", password_hash="x", role="user", org_id=1),
            ])
            await db.flush()
            # alice 的会话（id=1）与 bob 的会话（id=2）
            db.add_all([
                ChatSession(id=1, user_id=1, title="alice 的会话"),
                ChatSession(id=2, user_id=2, title="bob 的会话"),
            ])
            await db.flush()
            # 会话 1：提问(1) + 回答(2)
            # 会话 2：提问(3) + 回答(4)  —— 用来测"跨用户"
            db.add_all([
                Message(id=1, session_id=1, role="user", content="A8 多大面积？"),
                Message(id=2, session_id=1, role="assistant", content="A8 适用 30-40 平米。"),
                Message(id=3, session_id=2, role="user", content="S10 电池多大？"),
                Message(id=4, session_id=2, role="assistant", content="S10 电池 5000mAh。"),
            ])
            await db.commit()

    async def _call(self, user_id: int, session_id: int, message_id: int, feedback: int,
                    note: str | None = None):
        """直接调用接口函数（不走 HTTP 层），返回落库后的消息对象。"""
        from app.api.v1.sessions import set_message_feedback
        async with self.Session() as db:
            user = await db.get(User, user_id)
            payload = FeedbackRequest(feedback=feedback, note=note)
            return await set_message_feedback(session_id, message_id, payload, user, db)

    # ================================================== 正常路径

    def test_like_is_persisted(self):
        """点赞要真的写进数据库（1）。"""
        async def t():
            got = await self._call(1, 1, 2, 1)
            self.assertEqual(got.feedback, 1)
            async with self.Session() as db:
                row = await db.get(Message, 2)
                self.assertEqual(row.feedback, 1, "点赞没有落库")
        _run(t())

    def test_dislike_with_note_is_persisted(self):
        """点踩要落库（-1），并带上补充说明。"""
        async def t():
            got = await self._call(1, 1, 2, -1, note="引用的是过期资料")
            self.assertEqual(got.feedback, -1)
            async with self.Session() as db:
                row = await db.get(Message, 2)
                self.assertEqual(row.feedback, -1)
                self.assertEqual(row.feedback_note, "引用的是过期资料")
        _run(t())

    def test_cancel_clears_to_null(self):
        """取消评价要存成 NULL，而不是存一个 0。

        为什么较真这一点：如果存 0，那"用户从没评价过"和"用户取消过评价"
        在数据库里就分不清了，统计"不满意率"时会算错分母。
        """
        async def t():
            await self._call(1, 1, 2, 1)
            got = await self._call(1, 1, 2, 0)
            self.assertIsNone(got.feedback)
            async with self.Session() as db:
                row = await db.get(Message, 2)
                self.assertIsNone(row.feedback, "取消评价后应是 NULL")
        _run(t())

    def test_switch_from_like_to_dislike(self):
        """先赞后踩：以最后一次为准（不能同时是赞和踩）。"""
        async def t():
            await self._call(1, 1, 2, 1)
            got = await self._call(1, 1, 2, -1)
            self.assertEqual(got.feedback, -1)
            async with self.Session() as db:
                self.assertEqual((await db.get(Message, 2)).feedback, -1)
        _run(t())

    def test_switching_to_like_drops_old_note(self):
        """改成点赞时，之前点踩写的说明要清掉（否则留着一句骂人的备注很奇怪）。"""
        async def t():
            await self._call(1, 1, 2, -1, note="答错了")
            got = await self._call(1, 1, 2, 1)
            self.assertEqual(got.feedback, 1)
            self.assertIsNone(got.feedback_note)
        _run(t())

    # ================================================== 越权路径（重点）

    def test_cannot_rate_other_users_message(self):
        """给**别人的**回答打分必须 404（不是 403——403 等于承认"这条消息存在"）。"""
        async def t():
            # alice(id=1) 试图给 bob 会话(2) 里的回答(4)打分
            with self.assertRaises(HTTPException) as ctx:
                await self._call(1, 2, 4, 1)
            self.assertEqual(ctx.exception.status_code, 404)
            # 且库里那条消息不能被改动
            async with self.Session() as db:
                self.assertIsNone((await db.get(Message, 4)).feedback, "越权却改动了别人的数据")
        _run(t())

    def test_cannot_rate_message_from_other_session(self):
        """拿**自己的会话 id** 配**别的会话的消息 id**（拼装参数）必须 404。"""
        async def t():
            with self.assertRaises(HTTPException) as ctx:
                await self._call(1, 1, 4, 1)  # 会话1 是我的，消息4 不是
            self.assertEqual(ctx.exception.status_code, 404)
        _run(t())

    def test_cannot_rate_own_question(self):
        """只能评价 AI 回答；给自己的提问打分要 400。"""
        async def t():
            with self.assertRaises(HTTPException) as ctx:
                await self._call(1, 1, 1, 1)  # 消息1 是 alice 自己的提问
            self.assertEqual(ctx.exception.status_code, 400)
        _run(t())

    def test_missing_message_is_404(self):
        async def t():
            with self.assertRaises(HTTPException) as ctx:
                await self._call(1, 1, 9999, 1)
            self.assertEqual(ctx.exception.status_code, 404)
        _run(t())

    # ================================================== 入参校验

    def test_invalid_feedback_value_rejected(self):
        """只认 1 / -1 / 0，其它数字（例如 5）必须被入参校验拦下。"""
        for bad in (5, -3, 2):
            with self.assertRaises(ValidationError):
                FeedbackRequest(feedback=bad)

    def test_allowed_values_pass_validation(self):
        """反向对照：合法的三个值必须放行（防止把校验写得太严把正常请求也挡了）。"""
        for good in (1, -1, 0):
            self.assertEqual(FeedbackRequest(feedback=good).feedback, good)

    # ================================================== 出口防泄漏

    def test_message_out_does_not_leak_internals(self):
        """对外消息模型里不能出现密码哈希之类的内部字段。"""
        from app.schemas.chat import MessageOut
        fields = set(MessageOut.model_fields)
        for bad in ("password_hash", "password"):
            self.assertNotIn(bad, fields)
        self.assertIn("feedback", fields, "前端要靠 feedback 字段回显赞/踩状态")


if __name__ == "__main__":
    unittest.main()
