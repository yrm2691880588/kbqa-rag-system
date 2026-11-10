"""对外 API 与 API Key 测试（P1-G）。

这个文件在测什么（大白话）：
    企业要把我们的问答接进自家的客服系统/办公系统，程序没法像人一样"登录"，
    所以要给它一把**长期有效、可随时吊销**的钥匙 —— 这就是 API Key。

    几件必须较真的事（每一条都对应一次真实的踩坑可能）：
      ① **数据库里不能有明文钥匙**。只存一串"指纹"（哈希）。
         对比的时候拿指纹比，比不出来就说明钥匙是假的。
         真存了明文，一旦数据库被拖走，别人就能拿你的钥匙刷你的钱。
      ② **钥匙只能开自己家的门**。A 公司的钥匙绝不能看到 B 公司的知识库。
         即使请求里硬塞 B 公司的知识库编号，也要被自动剔除。
      ③ **吊销 = 立刻失效**，而且是"打标记"不是"删记录"——
         删了就看不出"这把钥匙是谁开的、用过多少次"了。
      ④ **失败提示语必须统一**。不能区分"钥匙不存在""已吊销""已过期"，
         否则等于给试探者当向导。
      ⑤ **JWT 和 API Key 不能串味**。两者都放在 Authorization: Bearer 里，
         靠 kbqa_ 前缀区分；拿一个登录令牌来当钥匙用，必须被拒。

运行方式（backend 目录下）：
    python -m unittest tests.test_api_key -v
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
from starlette.requests import Request

from app.core import api_key_auth as ka
from app.core.tenancy import accessible_kb_ids_for_org, resolve_kb_scope_for_org
from app.db.base import Base
from app.models.api_key import ApiKey
from app.models.knowledge_base import KnowledgeBase
from app.models.organization import Organization
from app.models.user import User
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreatedOut, ApiKeyOut


def _run(coro):
    return asyncio.run(coro)


def _req(headers: dict[str, str]) -> Request:
    """造一个最小的 HTTP 请求对象，只为了给鉴权函数读请求头。

    为什么不真起一个 HTTP 服务：那样慢、且要占端口；
    鉴权函数只关心请求头，直接构造即可（等价于真实请求进入时的样子）。
    """
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    return Request({
        "type": "http", "method": "GET", "path": "/api/v1/open/chat",
        "headers": raw, "query_string": b"", "scheme": "http", "server": ("test", 80),
    })


class ApiKeyTestCase(unittest.TestCase):
    """公共脚手架：两家客户组织 + 三个典型账号 + 各一个知识库。"""

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
                Organization(id=3, name="C公司已停用", invite_code="code-c", is_active=False),
            ])
            await db.flush()
            db.add_all([
                User(id=1, username="admin_a", password_hash="x", role="admin", org_id=1),
                User(id=2, username="admin_b", password_hash="x", role="admin", org_id=2),
                User(id=3, username="member_a", password_hash="x", role="user", org_id=1),
                User(id=9, username="root", password_hash="x", role="admin",
                     org_id=None, is_superadmin=True),
            ])
            await db.flush()
            db.add_all([
                KnowledgeBase(id=1, name="A家手册", org_id=1),
                KnowledgeBase(id=2, name="B家手册", org_id=2),
                KnowledgeBase(id=3, name="没主的库", org_id=None),
            ])
            await db.commit()

    # ---------- 小工具 ----------

    async def _user(self, uid: int) -> User:
        async with self.Session() as db:
            return await db.get(User, uid)

    async def _create(self, uid: int, **kw):
        """以某个账号的身份创建一把钥匙，返回接口的返回对象。"""
        async with self.Session() as db:
            user = await db.get(User, uid)
            from app.api.v1.api_keys import create_api_key
            return await create_api_key(ApiKeyCreate(**kw), user, db)

    async def _list(self, uid: int, org_id: int | None = None):
        async with self.Session() as db:
            user = await db.get(User, uid)
            from app.api.v1.api_keys import list_api_keys
            return await list_api_keys(user, db, org_id=org_id)

    async def _revoke(self, uid: int, key_id: int):
        async with self.Session() as db:
            user = await db.get(User, uid)
            from app.api.v1.api_keys import revoke_api_key
            return await revoke_api_key(key_id, user, db)

    async def _auth(self, raw: str | None, header: str = "authorization"):
        """模拟一次带钥匙的请求，返回鉴权结果（或抛出 HTTPException）。"""
        headers = {}
        if raw is not None:
            headers[header] = raw if header != "authorization" else f"Bearer {raw}"
        async with self.Session() as db:
            return await ka.require_api_key(_req(headers), db)

    # ================================================== 基础工具函数

    def test_hash_is_stable_and_different(self):
        """同一个钥匙算出来的指纹必须一样；不同钥匙的指纹必须不一样。"""
        a = ka.hash_key("kbqa_aaa")
        b = ka.hash_key("kbqa_aaa")
        c = ka.hash_key("kbqa_aab")
        self.assertEqual(a, b, "同样的钥匙算出两个指纹，比对永远失败")
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 64, "sha256 十六进制应该是 64 位")

    def test_looks_like_api_key(self):
        """靠前缀把 API Key 和登录令牌区分开。"""
        self.assertTrue(ka.looks_like_api_key("kbqa_abcdef"))
        # JWT 是三段点号分隔的，不该被误认为 API Key
        self.assertFalse(ka.looks_like_api_key("eyJhbGciOi.payload.signature"))
        self.assertFalse(ka.looks_like_api_key(""))
        self.assertFalse(ka.looks_like_api_key(None))

    def test_extract_raw_key_two_styles(self):
        """两种写法都要认：Authorization: Bearer，以及 X-API-Key。"""
        self.assertEqual(
            ka._extract_raw_key(_req({"Authorization": "Bearer kbqa_xyz"})), "kbqa_xyz"
        )
        self.assertEqual(
            ka._extract_raw_key(_req({"X-API-Key": "kbqa_xyz"})), "kbqa_xyz"
        )
        # 带一个不认识的 Bearer 令牌（例如登录用的 JWT）→ 不当成 API Key
        self.assertIsNone(ka._extract_raw_key(_req({"Authorization": "Bearer eyJhbG.xx.yy"})))
        self.assertIsNone(ka._extract_raw_key(_req({})))

    # ================================================== 创建

    def test_create_returns_plaintext_only_once(self):
        """创建时返回明文（只此一次），数据库里**必须没有明文**。"""
        async def t():
            got = await self._create(1, name="客服系统")
            self.assertTrue(got.key.startswith("kbqa_"), "钥匙没有固定前缀")
            self.assertEqual(got.key_prefix, got.key[:12], "展示用前缀应当是明文前 12 位")
            self.assertEqual(got.org_id, 1)
            self.assertEqual(got.name, "客服系统")
            self.assertFalse(got.revoked)
            self.assertIn("只显示这一次", got.message)

            # 数据库里：存的是指纹，不是明文
            async with self.Session() as db:
                row = await db.get(ApiKey, got.id)
                self.assertEqual(row.key_hash, ka.hash_key(got.key))
                self.assertNotEqual(row.key_hash, got.key)

            # 把整行的每个字段都翻一遍，确认没有任何一列等于明文（防止哪天有人加了个 key 列）
            async with self.Session() as db:
                row = await db.get(ApiKey, got.id)
                for col in ("name", "key_hash", "key_prefix"):
                    self.assertNotEqual(getattr(row, col), got.key, f"{col} 列疑似存了明文")
        _run(t())

    def test_list_and_detail_never_expose_plaintext(self):
        """列表/出口模型里不能有明文字段（否则等于钥匙到处乱飞）。"""
        self.assertNotIn("key", ApiKeyOut.model_fields, "列表项居然带明文 key 字段")
        self.assertIn("key", ApiKeyCreatedOut.model_fields)
        # 列表项的字段必须都能从数据库对象上取到（否则接口会 500）
        async def t():
            got = await self._create(1, name="k1")
            rows = await self._list(1)
            outs = [ApiKeyOut.model_validate(r) for r in rows]
            self.assertEqual(len(outs), 1)
            self.assertEqual(outs[0].id, got.id)
        _run(t())

    def test_admin_creates_for_own_org_ignoring_requested(self):
        """组织管理员只能给自己组织开钥匙；请求里填了别家的 id 也必须被忽略。"""
        async def t():
            got = await self._create(1, name="伪装", org_id=2)
            self.assertEqual(got.org_id, 1, "组织管理员把钥匙开到了别家组织")
        _run(t())

    def test_superadmin_must_specify_org(self):
        """平台超管自己不属于任何组织，必须明确说清楚给哪家开。"""
        async def t():
            with self.assertRaises(HTTPException) as ctx:
                await self._create(9, name="没指定")
            self.assertEqual(ctx.exception.status_code, 400)

            got = await self._create(9, name="给B家", org_id=2)
            self.assertEqual(got.org_id, 2)
        _run(t())

    def test_cannot_create_for_missing_or_disabled_org(self):
        """组织不存在 → 404；组织已停用 → 400（欠费停服的客户不该还能开新钥匙）。"""
        async def t():
            with self.assertRaises(HTTPException) as ctx:
                await self._create(9, org_id=999)
            self.assertEqual(ctx.exception.status_code, 404)

            with self.assertRaises(HTTPException) as ctx2:
                await self._create(9, org_id=3)
            self.assertEqual(ctx2.exception.status_code, 400)
        _run(t())

    def test_expires_days_sets_expiry(self):
        """填了"多少天后过期"就要真的记上过期时间。"""
        async def t():
            got = await self._create(1, name="临时", expires_days=30)
            self.assertIsNotNone(got.expires_at)
            if got.expires_at.tzinfo is None:
                exp = got.expires_at.replace(tzinfo=dt.timezone.utc)
            else:
                exp = got.expires_at
            delta = exp - dt.datetime.now(dt.timezone.utc)
            self.assertTrue(dt.timedelta(days=29) < delta < dt.timedelta(days=31),
                            f"过期时间不对：{delta}")
        _run(t())

    # ================================================== 鉴权

    def test_valid_key_authenticates(self):
        """好钥匙能通过鉴权，并带回它所属的组织。"""
        async def t():
            got = await self._create(1, name="客服")
            client = await self._auth(got.key)
            self.assertEqual(client.org_id, 1)
            self.assertEqual(client.key_id, got.id)
            self.assertEqual(client.name, "客服")
        _run(t())

    def test_unknown_revoked_expired_all_401_with_same_message(self):
        """不存在 / 已吊销 / 已过期 → 一律 401，且**提示语一字不差**。

        话术统一是刻意的：区分开就等于告诉试探者"这个钥匙存在，只是过期了"。
        """
        async def t():
            got = await self._create(1, name="待吊销")
            unknown = await self._raise(lambda: self._auth("kbqa_" + "z" * 30))

            await self._revoke(1, got.id)
            revoked = await self._raise(lambda: self._auth(got.key))

            expired_key = await self._create(1, name="已过期")
            async with self.Session() as db:
                row = await db.get(ApiKey, expired_key.id)
                row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
                await db.commit()
            expired = await self._raise(lambda: self._auth(expired_key.key))

            self.assertEqual([unknown.status_code, revoked.status_code, expired.status_code],
                             [401, 401, 401])
            self.assertEqual(unknown.detail, revoked.detail)
            self.assertEqual(revoked.detail, expired.detail)
        _run(t())

    async def _raise(self, coro_factory):
        """跑一段应当抛 HTTPException 的代码，把异常本身取出来比对。"""
        try:
            await coro_factory()
        except HTTPException as exc:
            return exc
        self.fail("本该被拒绝的调用居然通过了")

    def test_missing_key_message_is_helpful(self):
        """完全没带钥匙时，提示语要告诉对接方怎么带（这是唯一可以区分的一种情况）。"""
        async def t():
            exc = await self._raise(lambda: self._auth(None))
            self.assertEqual(exc.status_code, 401)
            self.assertIn("Authorization", exc.detail)
        _run(t())

    def test_jwt_is_not_accepted_as_api_key(self):
        """拿登录令牌冒充钥匙必须被拒（两者都放在 Bearer 里，容易串味）。"""
        async def t():
            exc = await self._raise(lambda: self._auth("eyJhbGciOiJIUzI1NiJ9.abc.def"))
            self.assertEqual(exc.status_code, 401)
        _run(t())

    def test_use_is_recorded(self):
        """每用一次，调用次数 +1、最后使用时间被写上（发现异常刷量靠它）。"""
        async def t():
            got = await self._create(1, name="统计")
            await self._auth(got.key)
            await self._auth(got.key)
            await self._auth(got.key)
            async with self.Session() as db:
                row = await db.get(ApiKey, got.id)
                self.assertEqual(row.use_count, 3, "调用次数没记上")
                self.assertIsNotNone(row.last_used_at, "最后使用时间没记上")
        _run(t())

    # ================================================== 列表与吊销

    def test_list_is_scoped_to_own_org(self):
        """列表只列自己组织的钥匙。"""
        async def t():
            await self._create(1, name="A1")
            await self._create(1, name="A2")
            await self._create(2, name="B1")

            a = await self._list(1)
            b = await self._list(2)
            self.assertEqual(sorted(x.name for x in a), ["A1", "A2"])
            self.assertEqual([x.name for x in b], ["B1"])

            # 超管可以指定看哪家
            root_all = await self._list(9, org_id=1)
            self.assertEqual(sorted(x.name for x in root_all), ["A1", "A2"])
        _run(t())

    def test_revoke_marks_not_deletes(self):
        """吊销是"打标记"，记录必须还在（要能查出是谁开的、用过多少次）。"""
        async def t():
            got = await self._create(1, name="要吊销")
            await self._revoke(1, got.id)
            async with self.Session() as db:
                row = await db.get(ApiKey, got.id)
                self.assertIsNotNone(row, "吊销把记录删掉了，历史查不到了")
                self.assertTrue(row.revoked, "吊销标记没打上")

            exc = await self._raise(lambda: self._auth(got.key))
            self.assertEqual(exc.status_code, 401, "吊销之后钥匙还能用")
        _run(t())

    def test_revoke_other_org_returns_404(self):
        """吊销别人家的钥匙 → 404（不承认它存在）。"""
        async def t():
            got = await self._create(2, name="B家钥匙")
            with self.assertRaises(HTTPException) as ctx:
                await self._revoke(1, got.id)
            self.assertEqual(ctx.exception.status_code, 404)
            _ = await self._user(3)

            # 而且它必须**没被真的吊销**（越权尝试不该产生副作用）
            async with self.Session() as db:
                row = await db.get(ApiKey, got.id)
                self.assertFalse(row.revoked, "越权吊销居然生效了")
        _run(t())

    def test_revoke_missing_returns_404(self):
        """吊销一个不存在的钥匙 → 404。"""
        async def t():
            with self.assertRaises(HTTPException) as ctx:
                await self._revoke(1, 12345)
            self.assertEqual(ctx.exception.status_code, 404)
        _run(t())

    # ================================================== 组织维度的隔离收敛

    def test_org_scope_is_never_none(self):
        """**关键**：按组织收敛范围时**永远不返回 None**。

        因为 None 在本项目里的含义是"不限制"。一旦这里返回了 None，
        下游会把它当成"可以看全部"，企业 A 的钥匙就能读到企业 B 的库 ——
        这是最严重的一类事故，所以单独钉一条。
        """
        async def t():
            async with self.Session() as db:
                self.assertEqual(await resolve_kb_scope_for_org(db, 1, None), [1])
                self.assertEqual(await resolve_kb_scope_for_org(db, 2, None), [2])
                # 不存在的组织 / 无归属组织：空列表（什么都看不到），而不是 None
                for bad in (999, None):
                    got = await resolve_kb_scope_for_org(db, bad, None)
                    self.assertEqual(got, [], f"org={bad} 应该什么都看不到")
                    self.assertIsNotNone(got)
        _run(t())

    def test_org_scope_drops_foreign_kb_ids(self):
        """请求里塞了别家的知识库编号 → 静默剔除（不报错，免得泄露对方存在）。"""
        async def t():
            async with self.Session() as db:
                got = await resolve_kb_scope_for_org(db, 1, [1, 2, 3])
                self.assertEqual(got, [1], "越权的知识库编号没有被剔除")
                # 全塞别家的 → 空列表
                self.assertEqual(await resolve_kb_scope_for_org(db, 1, [2, 3]), [])
        _run(t())

    def test_org_scope_ignores_unowned_kb(self):
        """没有归属组织的知识库，任何组织都看不到（宁可不给，不能错给）。"""
        async def t():
            async with self.Session() as db:
                self.assertEqual(await accessible_kb_ids_for_org(db, 1), [1])
                self.assertEqual(await accessible_kb_ids_for_org(db, None), [])
        _run(t())


if __name__ == "__main__":
    unittest.main()
