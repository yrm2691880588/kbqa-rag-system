"""忘记密码 / 重置密码 接口测试。

这个文件在测什么（大白话）：
    用户忘记密码，以前只能找管理员手动改数据库。现在加了一条自助通道：
    「输手机号 + 答图形验证码 → 收短信验证码 → 用它换新密码」。

    这件事做错的方式有两种，都很严重，所以都要测：
      ① **把"这个手机号注册过没有"说漏了** —— 那么任何人都能拿这份接口
         批量试探手机号，等于送出一份用户名单。所以本文件专门比对
         "账号存在"和"账号不存在"两种情况下，返回的文字必须**一模一样**。
      ② **改完密码，偷号的人还留在里面** —— 重置密码最常见的动机就是账号被盗。
         如果旧设备的登录凭证还能用，改密码就是白改。所以要断言
         "重置成功后，该账号所有未失效的登录凭证都被作废了"。

运行方式（backend 目录下）：
    python -m unittest tests.test_password_reset -v
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password, verify_password
from app.db.base import Base
from app.models.organization import Organization
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.models.verification_code import VerificationCode

FIXED_CAPTCHA = "ABCD"


def _run(coro):
    return asyncio.run(coro)


class PasswordResetTestCase(unittest.TestCase):
    """公共脚手架：一个绑了手机号的老用户 + 一个没绑手机号的用户。"""

    PHONE = "13800001111"
    PASSWORD = "OldPass123"

    def setUp(self):
        # 把图形验证码的随机文字固定下来，否则测试无法知道答案
        from app.services import auth_guard
        self._orig_gen = auth_guard._generate_captcha_text
        auth_guard._generate_captcha_text = lambda length=4: FIXED_CAPTCHA

        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        _run(self._seed())

    def tearDown(self):
        from app.services import auth_guard
        auth_guard._generate_captcha_text = self._orig_gen

    async def _seed(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.Session() as db:
            db.add(Organization(id=1, name="A公司", invite_code="code-a"))
            await db.flush()
            db.add_all([
                User(id=1, username="alice", password_hash=hash_password(self.PASSWORD),
                     role="user", org_id=1, phone=self.PHONE),
                User(id=2, username="nophone", password_hash=hash_password(self.PASSWORD),
                     role="user", org_id=1, phone=None),
            ])
            await db.flush()
            # 给 alice 预置两个"还没失效"的登录凭证，用来验证重置后被作废
            db.add_all([
                RefreshToken(id=1, user_id=1, token_hash="h" * 40, expires_at=_far_future(), revoked=False),
                RefreshToken(id=2, user_id=1, token_hash="i" * 40, expires_at=_far_future(), revoked=False),
                # 反向对照：本来就已作废的，不需要它来凑数
                RefreshToken(id=3, user_id=1, token_hash="j" * 40, expires_at=_far_future(), revoked=True),
            ])
            await db.commit()

    # ---------- 小工具 ----------

    async def _get_captcha(self, db):
        """要一张图形验证码，返回 (captcha_id, 正确答案)。"""
        from app.services import auth_guard
        captcha_id, _img = await auth_guard.issue_captcha(db)
        return captcha_id, FIXED_CAPTCHA

    async def _forgot(self, phone: str, captcha_id: str, captcha_code: str):
        from app.api.v1.auth import forgot_password
        from app.schemas.user import ForgotPasswordRequest
        async with self.Session() as db:
            payload = ForgotPasswordRequest(
                phone=phone, captcha_id=captcha_id, captcha_code=captcha_code
            )
            return await forgot_password(payload, _FakeRequest(), db)

    async def _reset(self, phone: str, code: str, new_password: str):
        from app.api.v1.auth import reset_password
        from app.schemas.user import ResetPasswordRequest
        async with self.Session() as db:
            payload = ResetPasswordRequest(phone=phone, code=code, new_password=new_password)
            return await reset_password(payload, db)

    async def _sms_code_for(self, phone: str) -> str | None:
        """从库里读回最近一条未使用的短信验证码明文（测试专用，靠 mock 固定值反推）。"""
        return "123456"  # sms_provider=mock 时后端固定用这个码

    # ================================================== 第一步：申请重置

    def test_forgot_rejects_wrong_captcha(self):
        """图形验证码答错 → 400，且不发短信（防脚本批量刷短信）。"""
        async def t():
            async with self.Session() as db:
                captcha_id, _ = await self._get_captcha(db)
            with self.assertRaises(HTTPException) as ctx:
                await self._forgot(self.PHONE, captcha_id, "ZZZZ")
            self.assertEqual(ctx.exception.status_code, 400)
            async with self.Session() as db:
                rows = (await db.scalars(
                    select(VerificationCode).where(VerificationCode.kind == "sms")
                )).all()
                self.assertEqual(rows, [], "图形码答错却把短信发出去了")
        _run(t())

    def test_forgot_sends_code_for_existing_account(self):
        """账号存在 → 正常发码（mock 模式下会把测试码回给前端，方便本地开发）。"""
        async def t():
            async with self.Session() as db:
                captcha_id, answer = await self._get_captcha(db)
            got = await self._forgot(self.PHONE, captcha_id, answer)
            self.assertEqual(got.dev_code, "123456")
            async with self.Session() as db:
                rows = (await db.scalars(
                    select(VerificationCode).where(VerificationCode.kind == "sms")
                )).all()
                self.assertEqual(len(rows), 1, "应发出 1 条短信验证码")
        _run(t())

    def test_forgot_does_not_reveal_account_existence(self):
        """**核心安全断言**：账号存在与否，对外话术必须一字不差。

        如果两者文案不同（比如一个说"已发送"另一个说"该手机号未注册"），
        这份接口就成了"手机号是否注册过"的批量查询工具。
        """
        async def t():
            async with self.Session() as db:
                c1, a1 = await self._get_captcha(db)
            existing = await self._forgot(self.PHONE, c1, a1)

            async with self.Session() as db:
                c2, a2 = await self._get_captcha(db)
            missing = await self._forgot("13900009999", c2, a2)  # 这个号没注册

            self.assertEqual(
                existing.message, missing.message,
                "账号存在/不存在返回的提示不同，等于泄露了『这个手机号注册过』",
            )
            self.assertIsNone(missing.dev_code, "账号不存在却返回了验证码")
        _run(t())

    def test_forgot_does_not_send_when_account_missing(self):
        """账号不存在 → 不能真发短信（否则被当成免费短信通道）。"""
        async def t():
            async with self.Session() as db:
                c, a = await self._get_captcha(db)
            await self._forgot("13900009999", c, a)
            async with self.Session() as db:
                rows = (await db.scalars(
                    select(VerificationCode).where(VerificationCode.kind == "sms")
                )).all()
                self.assertEqual(rows, [], "账号不存在却发了短信")
        _run(t())

    # ================================================== 第二步：重置

    def test_reset_rejects_wrong_code(self):
        async def t():
            with self.assertRaises(HTTPException) as ctx:
                await self._reset(self.PHONE, "000000", "NewPass123")
            self.assertEqual(ctx.exception.status_code, 400)
        _run(t())

    def test_reset_rejects_weak_password_without_burning_code(self):
        """密码太弱 → 400，且**验证码不能被用掉**。

        为什么较真：如果这里把码消耗了，用户只是密码填得不合规范，
        就得重新等 60 秒再收一条短信。这是很常见的体验坑。
        """
        async def t():
            await self._send_code()
            with self.assertRaises(HTTPException) as ctx:
                await self._reset(self.PHONE, "123456", "12345678")  # 纯数字，不合格
            self.assertEqual(ctx.exception.status_code, 400)

            # 码没被烧掉：改用合格密码应该能一次成功
            got = await self._reset(self.PHONE, "123456", "NewPass123")
            self.assertTrue(got.message)
        _run(t())

    def test_reset_rejects_password_containing_username(self):
        """新密码里含用户名 → 拒绝（防"用户名+123"这种偷懒组合）。"""
        async def t():
            await self._send_code()
            with self.assertRaises(HTTPException) as ctx:
                await self._reset(self.PHONE, "123456", "alice2026x")
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("用户名", ctx.exception.detail)
        _run(t())

    def test_reset_changes_password(self):
        """正常重置：新密码能登录，旧密码不能。"""
        async def t():
            await self._send_code()
            await self._reset(self.PHONE, "123456", "NewPass123")
            async with self.Session() as db:
                u = await db.get(User, 1)
                self.assertTrue(verify_password("NewPass123", u.password_hash), "新密码不生效")
                self.assertFalse(verify_password(self.PASSWORD, u.password_hash), "旧密码还能用")
        _run(t())

    def test_reset_revokes_all_active_tokens(self):
        """**核心安全断言**：重置后该账号所有"还没失效"的登录凭证必须全部作废。

        场景：账号被盗 → 用户改密码。如果盗号者的登录状态还在，改密码等于没改。
        """
        async def t():
            await self._send_code()
            await self._reset(self.PHONE, "123456", "NewPass123")
            async with self.Session() as db:
                alive = (await db.scalars(
                    select(RefreshToken).where(
                        RefreshToken.user_id == 1, RefreshToken.revoked.is_(False)
                    )
                )).all()
                self.assertEqual(alive, [], f"重置后仍有 {len(alive)} 个登录凭证未失效")
                # 反向对照：本来就已经作废的那条也仍然是作废状态（没被误改回来）
                self.assertTrue((await db.get(RefreshToken, 3)).revoked)
        _run(t())

    def test_reset_code_is_single_use(self):
        """同一个验证码不能用第二次（防重放）。"""
        async def t():
            await self._send_code()
            await self._reset(self.PHONE, "123456", "NewPass123")
            with self.assertRaises(HTTPException) as ctx:
                await self._reset(self.PHONE, "123456", "AnotherPass123")
            self.assertEqual(ctx.exception.status_code, 400)
        _run(t())

    def test_reset_for_unknown_phone_is_rejected(self):
        """没绑定账号的手机号 + 任意码 → 400（统一提示，不透露账号情况）。"""
        async def t():
            with self.assertRaises(HTTPException) as ctx:
                await self._reset("13900009999", "123456", "NewPass123")
            self.assertEqual(ctx.exception.status_code, 400)
        _run(t())

    # ---------- 供上面用例复用：先申请一条验证码 ----------

    async def _send_code(self):
        async with self.Session() as db:
            captcha_id, answer = await self._get_captcha(db)
        await self._forgot(self.PHONE, captcha_id, answer)


class _FakeRequest:
    """伪造一个请求对象：接口只用到 client.host（取 IP），其余不需要。"""

    class _Client:
        host = "127.0.0.1"

    client = _Client()


def _far_future():
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)


if __name__ == "__main__":
    unittest.main()
