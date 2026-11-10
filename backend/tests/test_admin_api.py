"""管理后台接口测试（P1-C）：成员管理 + 问答日志。

这个文件在测什么（大白话）：
    以前客户管理员在后台什么都做不了：加不了人、停不了号、帮人找回密码也没办法，
    问答日志记了却看不到。这次把这三件事补上。

    补这些功能时最容易出的事故是**权限越界**，所以本文件一半篇幅在测"越权会被挡住"：
      · 组织管理员不能动别家公司的人（否则等于跨公司接管账号）；
      · 组织管理员不能把自己或别人提成"平台超管"（否则跨组织隔离的基础就塌了）；
      · 组织管理员不能看别家公司的问答日志（那里有客户真实问过的业务问题）。

运行方式（backend 目录下）：
    python -m unittest tests.test_admin_api -v
"""

import asyncio
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password, verify_password
from app.db.base import Base
from app.models.organization import Organization
from app.models.query_log import QueryLog
from app.models.refresh_token import RefreshToken
from app.models.user import User

OLD_PWD = "OldPass123"


def _run(coro):
    return asyncio.run(coro)


def _future():
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)


# ------------------------------------------------------------------
# 两个小包装：把"查询参数"的默认值补全。
#
# 为什么需要它们：FastAPI 里写成 `limit: int = Query(50, ge=1)` 的参数，
# 在**直接调用函数**（不走 HTTP）时，收到的其实是那个 Query 占位对象，
# 而不是 50 —— .limit(<Query 对象>) 会直接抛 TypeError。
# 走 HTTP 请求时框架会替我们解析，所以这不是产品缺陷，只是测试的调用方式问题。
# 有了这层包装，每个用例就不用次次手写一堆默认值了。
# ------------------------------------------------------------------

async def _users_api(db, operator, **overrides):
    from app.api.v1.users import list_users
    params = dict(org_id=None, keyword=None, limit=200)
    params.update(overrides)
    return await list_users(operator, db, **params)


async def _logs_api(db, operator, **overrides):
    from app.api.v1.query_logs import list_query_logs
    params = dict(answered=None, keyword=None, limit=50, offset=0)
    params.update(overrides)
    return await list_query_logs(operator, db, **params)


class AdminApiTestCase(unittest.TestCase):
    """公共脚手架：两家公司，各有管理员与成员，外加一个平台超管。"""

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
                # A 公司
                User(id=1, username="admin_a", password_hash=hash_password(OLD_PWD),
                     role="admin", org_id=1),
                User(id=2, username="alice", password_hash=hash_password(OLD_PWD),
                     role="user", org_id=1, nickname="小爱", phone="13800001111"),
                # B 公司
                User(id=3, username="admin_b", password_hash=hash_password(OLD_PWD),
                     role="admin", org_id=2),
                User(id=4, username="bob", password_hash=hash_password(OLD_PWD),
                     role="user", org_id=2),
                # 平台超管
                User(id=9, username="root", password_hash=hash_password(OLD_PWD),
                     role="admin", org_id=None, is_superadmin=True),
            ])
            await db.flush()
            # A 公司有 2 条日志（1 条答上、1 条没答上），B 公司 1 条
            db.add_all([
                QueryLog(id=1, user_id=2, session_id=1, question="A8 多大面积？",
                         answered=True, latency_ms=1200, retrieval=[{"doc_name": "A8.md", "score": 0.9}]),
                QueryLog(id=2, user_id=2, session_id=1, question="你们支持退货吗？",
                         answered=False, latency_ms=800, retrieval=[]),
                QueryLog(id=3, user_id=4, session_id=2, question="B公司的问题",
                         answered=True, latency_ms=900, retrieval=[]),
            ])
            # 给 alice 一个有效登录凭证，用来验证"停用/重置后会被踢下线"
            db.add(RefreshToken(id=1, user_id=2, token_hash="h" * 40,
                                expires_at=_future(), revoked=False))
            await db.commit()

    # ================================================== 成员列表

    def test_org_admin_sees_only_own_org(self):
        """组织管理员只能看到本组织的人 —— 别家公司有哪些员工属于商业信息。"""
        async def t():
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                rows = await _users_api(db, admin_a)
                ids = sorted(u.id for u in rows)
                self.assertEqual(ids, [1, 2], "看到了别家公司的成员")
        _run(t())

    def test_superadmin_can_scope_to_one_org(self):
        """平台超管可以指定组织；不指定就返回全部。"""
        async def t():
            async with self.Session() as db:
                root = await db.get(User, 9)
                scoped = sorted(u.id for u in await _users_api(db, root, org_id=2))
                self.assertEqual(scoped, [3, 4])
                all_ids = sorted(u.id for u in await _users_api(db, root))
                self.assertEqual(all_ids, [1, 2, 3, 4, 9])
        _run(t())

    def test_keyword_search(self):
        """按用户名/昵称/手机号都能搜到（客户几百人时不可能靠翻页找人）。"""
        async def t():
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                self.assertEqual([u.id for u in await _users_api(db, admin_a, keyword="小爱")], [2])
                self.assertEqual([u.id for u in await _users_api(db, admin_a, keyword="13800")], [2])
                self.assertEqual([u.id for u in await _users_api(db, admin_a, keyword="alice")], [2])
        _run(t())

    # ================================================== 改角色 / 启停

    def test_org_admin_can_promote_own_member(self):
        async def t():
            from app.api.v1.users import update_user
            from app.schemas.user import AdminUserUpdate
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                got = await update_user(2, AdminUserUpdate(role="admin"), admin_a, db)
                self.assertEqual(got.role, "admin")
        _run(t())

    def test_org_admin_cannot_touch_other_org_member(self):
        """**核心安全断言**：动别家公司的人 → 404（不是 403，不承认对方存在）。"""
        async def t():
            from app.api.v1.users import update_user
            from app.schemas.user import AdminUserUpdate
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                with self.assertRaises(HTTPException) as ctx:
                    await update_user(4, AdminUserUpdate(is_active=False), admin_a, db)
                self.assertEqual(ctx.exception.status_code, 404)

            # 而且 bob 必须还没被停用
            async with self.Session() as db:
                self.assertTrue((await db.get(User, 4)).is_active, "越权却真的把别人停用了")
        _run(t())

    def test_cannot_modify_self(self):
        """不能改自己：顺手把自己降级/停用，就再也进不了管理页了。"""
        async def t():
            from app.api.v1.users import update_user
            from app.schemas.user import AdminUserUpdate
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                with self.assertRaises(HTTPException) as ctx:
                    await update_user(1, AdminUserUpdate(is_active=False), admin_a, db)
                self.assertEqual(ctx.exception.status_code, 400)
        _run(t())

    def test_cannot_touch_superadmin_account(self):
        """平台超管的账号不接受组织侧管理（否则客户能处置运营方账号）。"""
        async def t():
            from app.api.v1.users import update_user
            from app.schemas.user import AdminUserUpdate
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                with self.assertRaises(HTTPException) as ctx:
                    await update_user(9, AdminUserUpdate(is_active=False), admin_a, db)
                self.assertEqual(ctx.exception.status_code, 404)
        _run(t())

    def test_role_cannot_be_escalated_to_superadmin(self):
        """**提权防护**：接口层就不接受 role=superadmin，从源头堵死。"""
        from app.schemas.user import AdminUserUpdate
        for bad in ("superadmin", "root", "owner", "ADMIN2"):
            with self.assertRaises(ValidationError):
                AdminUserUpdate(role=bad)
        # 反向对照：合法的两个角色必须放行（别把校验写太严把正常也挡了）
        self.assertEqual(AdminUserUpdate(role="admin").role, "admin")
        self.assertEqual(AdminUserUpdate(role="user").role, "user")

    def test_disable_also_revokes_tokens(self):
        """停用账号必须顺手踢下线，否则旧登录状态还能用到令牌过期。"""
        async def t():
            from app.api.v1.users import update_user
            from app.schemas.user import AdminUserUpdate
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                got = await update_user(2, AdminUserUpdate(is_active=False), admin_a, db)
                self.assertFalse(got.is_active)
                alive = (await db.scalars(
                    select(RefreshToken).where(
                        RefreshToken.user_id == 2, RefreshToken.revoked.is_(False)
                    )
                )).all()
                self.assertEqual(alive, [], "账号停用了，但登录凭证还没作废")
        _run(t())

    # ================================================== 管理员重置密码

    def test_admin_reset_password_generates_temp(self):
        """管理员重置：系统给随机临时密码，密码真的改了，旧凭证全失效。"""
        async def t():
            from app.api.v1.users import admin_reset_password
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                got = await admin_reset_password(2, admin_a, db)
                self.assertEqual(got.username, "alice")
                self.assertGreaterEqual(len(got.temp_password), 8)
                self.assertTrue(got.message)

            async with self.Session() as db:
                alice = await db.get(User, 2)
                self.assertTrue(verify_password(got.temp_password, alice.password_hash),
                                "临时密码登不上，等于重置失败")
                self.assertFalse(verify_password(OLD_PWD, alice.password_hash), "旧密码还能用")
                alive = (await db.scalars(
                    select(RefreshToken).where(
                        RefreshToken.user_id == 2, RefreshToken.revoked.is_(False)
                    )
                )).all()
                self.assertEqual(alive, [], "重置密码后旧登录状态仍然有效")
        _run(t())

    def test_cannot_reset_own_password_here(self):
        """不能给自己重置（那是"修改密码"的活，这里重置等于拿到了自己的明文密码）。"""
        async def t():
            from app.api.v1.users import admin_reset_password
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                with self.assertRaises(HTTPException) as ctx:
                    await admin_reset_password(1, admin_a, db)
                self.assertEqual(ctx.exception.status_code, 400)
        _run(t())

    def test_cannot_reset_other_org_member(self):
        async def t():
            from app.api.v1.users import admin_reset_password
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                with self.assertRaises(HTTPException) as ctx:
                    await admin_reset_password(4, admin_a, db)
                self.assertEqual(ctx.exception.status_code, 404)
        _run(t())

    def test_temp_password_passes_strength_check(self):
        """随机临时密码必须能过全站密码强度校验（否则用户改密码时会被自己的密码卡住）。"""
        from app.core.security import validate_password_strength
        async def t():
            from app.api.v1.users import admin_reset_password
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                for _ in range(5):     # 多跑几次，覆盖随机性
                    got = await admin_reset_password(2, admin_a, db)
                    ok, reason = validate_password_strength(got.temp_password, "alice")
                    self.assertTrue(ok, f"生成的临时密码不合规：{reason}")
        _run(t())

    # ================================================== 问答日志

    def test_query_logs_scoped_to_own_org(self):
        """**核心安全断言**：A 公司管理员看不到 B 公司用户问过什么问题。"""
        async def t():
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                rows = await _logs_api(db, admin_a)
                qs = [r.question for r in rows]
                self.assertIn("A8 多大面积？", qs)
                self.assertNotIn("B公司的问题", qs, "看到了别家公司的问答日志")
                self.assertEqual(len(rows), 2)
        _run(t())

    def test_query_logs_filter_unanswered(self):
        """能只看"没答上"的 —— 这是管理员最需要的清单。"""
        async def t():
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                rows = await _logs_api(db, admin_a, answered=False)
                self.assertEqual([r.question for r in rows], ["你们支持退货吗？"])
        _run(t())

    def test_query_log_stats(self):
        """概览统计的数字要对得上（总量/未答上/平均耗时）。"""
        async def t():
            from app.api.v1.query_logs import query_log_stats
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                s = await query_log_stats(admin_a, db)
                self.assertEqual(s.total, 2)
                self.assertEqual(s.answered, 1)
                self.assertEqual(s.unanswered, 1)
                self.assertEqual(s.avg_latency_ms, 1000)   # (1200+800)/2
        _run(t())

    def test_query_log_list_omits_heavy_retrieval(self):
        """列表**不能**带检索明细（那是原文片段，一条几十 KB，一页会把响应撑爆）。"""
        async def t():
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                rows = await _logs_api(db, admin_a)
                for r in rows:
                    self.assertNotIn("retrieval", r.model_dump(), "列表带上了沉重的检索明细")
                # 但要给出"命中几段"，够在列表上判断检索有没有结果
                self.assertEqual(rows[-1].hit_count, 1)
        _run(t())

    def test_query_log_detail_includes_retrieval(self):
        async def t():
            from app.api.v1.query_logs import get_query_log
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                got = await get_query_log(1, admin_a, db)
                self.assertIsNotNone(got.retrieval)
                self.assertEqual(got.username, "alice")
        _run(t())

    def test_cannot_read_other_org_log_detail(self):
        """日志详情也要"顺藤摸瓜"校验归属（日志 → 用户 → 组织）。"""
        async def t():
            from app.api.v1.query_logs import get_query_log
            async with self.Session() as db:
                admin_a = await db.get(User, 1)
                with self.assertRaises(HTTPException) as ctx:
                    await get_query_log(3, admin_a, db)   # 3 号是 B 公司的日志
                self.assertEqual(ctx.exception.status_code, 404)
        _run(t())

    def test_superadmin_sees_all_logs(self):
        async def t():
            async with self.Session() as db:
                root = await db.get(User, 9)
                rows = await _logs_api(db, root)
                self.assertEqual(len(rows), 3)
        _run(t())

    # ================================================== 普通成员不能进后台

    def test_normal_member_cannot_manage_users(self):
        """普通成员连管理接口的门都进不去（require_admin 先挡住）。"""
        async def t():
            from app.core.deps import require_admin
            async with self.Session() as db:
                alice = await db.get(User, 2)
                with self.assertRaises(HTTPException) as ctx:
                    await require_admin(alice)
                self.assertEqual(ctx.exception.status_code, 403)
        _run(t())


if __name__ == "__main__":
    unittest.main()
