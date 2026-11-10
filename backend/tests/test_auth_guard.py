"""登录守卫服务测试（auth_guard.py）：图形验证码 / 短信码 / 登录失败锁定。

用 SQLite 内存库跑真实数据库逻辑（StaticPool 共享同一连接，保证表存在且数据一致）。

运行方式（backend 目录下）：
    python -m unittest tests.test_auth_guard -v
"""
import asyncio
import base64
import io
import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.services import auth_guard as g


def _new_session_maker():
    """建一个内存 SQLite 的会话工厂（共享连接，跑完即弃）。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,  # 所有会话共用一个内存连接
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _run(coro):
    """同步入口跑 async 测试。"""
    return asyncio.run(coro)


class TestCaptcha(unittest.TestCase):
    """图形验证码：发放、错误拒绝、过期路径。"""

    def setUp(self):
        self.engine, self.session_factory = _new_session_maker()
        asyncio.run(self._create_tables())

    async def _create_tables(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def test_issue_returns_image(self):
        async def t():
            async with self.session_factory() as db:
                captcha_id, image = await g.issue_captcha(db)
                self.assertTrue(captcha_id and len(captcha_id) > 20)
                self.assertTrue(image.startswith("data:image/png;base64,"))
                # 解码后确认是有效 PNG
                raw = base64.b64decode(image.split(",", 1)[1])
                self.assertTrue(raw[:8].startswith(b"\x89PNG\r\n\x1a\n"))
        _run(t())

    def test_wrong_answer_rejected(self):
        async def t():
            async with self.session_factory() as db:
                captcha_id, _ = await g.issue_captcha(db)
                self.assertFalse(await g.consume_captcha(db, captcha_id, "XXXX"))
                self.assertFalse(await g.consume_captcha(db, captcha_id, ""))
                self.assertFalse(await g.consume_captcha(db, "no-such-id", "AB12"))
        _run(t())


class TestSmsCode(unittest.TestCase):
    """短信验证码：发放、冷却、校验、一次性。"""

    def setUp(self):
        self.engine, self.session_factory = _new_session_maker()
        asyncio.run(self._create_tables())

    async def _create_tables(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def test_issue_and_verify(self):
        async def t():
            async with self.session_factory() as db:
                ok, msg, dev = await g.issue_sms_code(db, "13800138000", "127.0.0.1")
                self.assertTrue(ok)
                self.assertEqual(dev, "123456")  # mock 模式固定测试码
                # 正确验证码可通过
                self.assertTrue(await g.verify_sms_code(db, "13800138000", "123456"))
                # 一次性：用后即焚，重复使用被拒
                self.assertFalse(await g.verify_sms_code(db, "13800138000", "123456"))
        _run(t())

    def test_wrong_code_rejected(self):
        async def t():
            async with self.session_factory() as db:
                await g.issue_sms_code(db, "13900139000", "127.0.0.1")
                self.assertFalse(await g.verify_sms_code(db, "13900139000", "654321"))
        _run(t())

    def test_cooldown_blocks_resend(self):
        async def t():
            async with self.session_factory() as db:
                ok, _, _ = await g.issue_sms_code(db, "13700137000", "127.0.0.1")
                self.assertTrue(ok)
                # 立即重发 → 冷却期拒绝
                ok2, msg2, _ = await g.issue_sms_code(db, "13700137000", "127.0.0.1")
                self.assertFalse(ok2)
                self.assertIn("太频繁", msg2)
        _run(t())

    def test_invalid_phone_rejected(self):
        self.assertFalse(g.is_valid_phone("12345"))
        self.assertFalse(g.is_valid_phone("12345678901"))
        self.assertFalse(g.is_valid_phone("23800138000"))
        self.assertTrue(g.is_valid_phone("13800138000"))


class TestLoginLock(unittest.TestCase):
    """登录失败锁定：5 次触发、清零解锁。"""

    def setUp(self):
        self.engine, self.session_factory = _new_session_maker()
        asyncio.run(self._create_tables())

    def tearDown(self):
        asyncio.run(self.engine.dispose())

    async def _create_tables(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def test_lock_after_5_failures(self):
        async def t():
            async with self.session_factory() as db:
                for _ in range(4):
                    await g.record_login_failure(db, "bob", "1.1.1.1")
                self.assertIsNone(await g.check_login_locked(db, "bob", "9.9.9.9"))
                # 第 5 次失败（换个 IP）→ 账号级锁定（防撞库：多 IP 轮着试也锁）
                await g.record_login_failure(db, "bob", "2.2.2.2")
                self.assertIsNotNone(await g.check_login_locked(db, "bob", "9.9.9.9"))
        _run(t())

    def test_clear_on_success(self):
        async def t():
            async with self.session_factory() as db:
                for _ in range(5):
                    await g.record_login_failure(db, "alice", "1.1.1.1")
                self.assertIsNotNone(await g.check_login_locked(db, "alice", "3.3.3.3"))
                await g.clear_login_failures(db, "alice")
                self.assertIsNone(await g.check_login_locked(db, "alice", "3.3.3.3"))
        _run(t())

    def test_ip_level_lock(self):
        async def t():
            async with self.session_factory() as db:
                # 同一 IP 对不同账号失败 20 次 → IP 级锁定
                for i in range(20):
                    await g.record_login_failure(db, f"user{i:02d}", "6.6.6.6")
                self.assertIsNotNone(await g.check_login_locked(db, "fresh_user", "6.6.6.6"))
        _run(t())


if __name__ == "__main__":
    unittest.main()
