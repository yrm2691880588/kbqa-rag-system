"""组织管理接口测试（的第二块验收：三级权限是否各就各位）。

这个文件在测什么（大白话）：
    系统里现在有三档人：
      · 平台超管   —— 你自己（系统运营方），能建客户组织、管所有组织；
      · 组织管理员 —— 客户公司里的管理员，只能管自己公司的知识库和成员；
      · 组织成员   —— 客户公司的普通员工，只能问答。
    测试要回答两个问题：
      ① 权限卡住了没有？（普通管理员绝不能建组织，否则隔离等于不存在）
      ② 敏感信息漏了没有？（邀请码是"进门的钥匙"，不能顺手在组织列表里带出去）

运行方式（backend 目录下）：
    python -m unittest tests.test_org_api -v
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

from app.db.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.organization import Organization
from app.models.user import User
from app.schemas.organization import OrgCreate, OrgUpdate


def _run(coro):
    return asyncio.run(coro)


class OrgApiTestCase(unittest.TestCase):
    """公共脚手架：两家客户公司 + 一个平台超管。"""

    def setUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        _run(self._seed())

    async def _seed(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.Session() as db:
            db.add_all([
                Organization(id=1, name="A公司", invite_code="code-a-original"),
                Organization(id=2, name="B公司", invite_code="code-b-original"),
            ])
            await db.flush()
            db.add_all([
                User(id=1, username="alice", password_hash="x", role="user", org_id=1),
                User(id=2, username="admin_a", password_hash="x", role="admin", org_id=1),
                User(id=3, username="admin_b", password_hash="x", role="admin", org_id=2),
                User(id=4, username="root", password_hash="x", role="admin",
                     org_id=None, is_superadmin=True),
                User(id=5, username="homeless", password_hash="x", role="user", org_id=None),
            ])
            await db.flush()
            db.add(KnowledgeBase(id=1, name="A的库", org_id=1))
            await db.commit()

    # ================================================== 第一档：超管依赖

    def test_require_superadmin_rejects_org_admin(self):
        """组织管理员**不能**建组织：否则自己给自己开一个客户，隔离就白做了。"""
        async def t():
            from app.core.deps import require_superadmin
            async with self.Session() as db:
                admin_a = await db.get(User, 2)
                with self.assertRaises(HTTPException) as ctx:
                    await require_superadmin(admin_a)
                self.assertEqual(ctx.exception.status_code, 403)
        _run(t())

    def test_require_superadmin_rejects_normal_member(self):
        async def t():
            from app.core.deps import require_superadmin
            async with self.Session() as db:
                alice = await db.get(User, 1)
                with self.assertRaises(HTTPException) as ctx:
                    await require_superadmin(alice)
                self.assertEqual(ctx.exception.status_code, 403)
        _run(t())

    def test_require_superadmin_allows_superadmin(self):
        async def t():
            from app.core.deps import require_superadmin
            async with self.Session() as db:
                root = await db.get(User, 4)
                self.assertEqual((await require_superadmin(root)).id, 4)
        _run(t())

    # ================================================== 建组织 / 列表

    def test_create_org_returns_fresh_invite_code(self):
        async def t():
            from app.api.v1.organizations import create_organization
            async with self.Session() as db:
                root = await db.get(User, 4)
                got = await create_organization(OrgCreate(name="C公司", description="新客户"), root, db)
                self.assertEqual(got.name, "C公司")
                self.assertTrue(got.invite_code)
                self.assertGreaterEqual(len(got.invite_code), 16, "邀请码不能太短")
                self.assertNotIn(got.invite_code, ("code-a-original", "code-b-original"))
        _run(t())

    def test_create_org_duplicate_name_rejected(self):
        async def t():
            from app.api.v1.organizations import create_organization
            async with self.Session() as db:
                root = await db.get(User, 4)
                with self.assertRaises(HTTPException) as ctx:
                    await create_organization(OrgCreate(name="A公司"), root, db)
                self.assertEqual(ctx.exception.status_code, 409)
        _run(t())

    def test_org_list_never_leaks_invite_code(self):
        """组织列表里**绝不能**带邀请码 —— 能看列表的人不该拿到"进门钥匙"。"""
        async def t():
            from app.api.v1.organizations import list_organizations
            async with self.Session() as db:
                root = await db.get(User, 4)
                rows = await list_organizations(root, db)
                self.assertGreaterEqual(len(rows), 2)
                for r in rows:
                    self.assertNotIn("invite_code", r.model_dump(), "组织列表泄露了邀请码")
                # 反向对照：创建时**必须**给（否则管理员拿不到码，没法拉人）
                from app.api.v1.organizations import create_organization
                created = await create_organization(OrgCreate(name="D公司"), root, db)
                self.assertIn("invite_code", created.model_dump())
        _run(t())

    def test_org_counts_are_scoped_per_org(self):
        async def t():
            from app.api.v1.organizations import list_organizations
            async with self.Session() as db:
                root = await db.get(User, 4)
                rows = {r.name: r for r in await list_organizations(root, db)}
                self.assertEqual(rows["A公司"].member_count, 2, "A公司应有 2 名成员(alice/admin_a)")
                self.assertEqual(rows["A公司"].kb_count, 1)
                self.assertEqual(rows["B公司"].member_count, 1)
                self.assertEqual(rows["B公司"].kb_count, 0, "B公司不应统计到 A 公司的知识库")
        _run(t())

    # ================================================== 查看自己的组织

    def test_my_org_visible_for_member(self):
        async def t():
            from app.api.v1.organizations import get_my_organization
            async with self.Session() as db:
                alice = await db.get(User, 1)
                got = await get_my_organization(alice, db)
                self.assertEqual(got.name, "A公司")
                self.assertNotIn("invite_code", got.model_dump())
        _run(t())

    def test_my_org_404_when_homeless(self):
        async def t():
            from app.api.v1.organizations import get_my_organization
            async with self.Session() as db:
                u = await db.get(User, 5)
                with self.assertRaises(HTTPException) as ctx:
                    await get_my_organization(u, db)
                self.assertEqual(ctx.exception.status_code, 404)
        _run(t())

    # ================================================== 邀请码可见性

    def test_org_admin_can_see_own_invite_code(self):
        """组织管理员能看到**自己组织**的邀请码（否则他没法拉同事进来）。"""
        async def t():
            from app.api.v1.organizations import get_my_invite_code
            async with self.Session() as db:
                admin_a = await db.get(User, 2)
                got = await get_my_invite_code(admin_a, db)
                self.assertEqual(got.org_id, 1)
                self.assertEqual(got.invite_code, "code-a-original")
        _run(t())

    def test_org_admin_cannot_see_other_org_code(self):
        """B 公司的管理员只能拿到 B 公司的码，拿不到 A 公司的。

        这是隔离的核心：邀请码是准入凭证，跨组织拿到就等于能往别人公司塞人。
        """
        async def t():
            from app.api.v1.organizations import get_my_invite_code
            async with self.Session() as db:
                admin_b = await db.get(User, 3)
                got = await get_my_invite_code(admin_b, db)
                self.assertEqual(got.org_id, 2)
                self.assertNotEqual(got.invite_code, "code-a-original", "拿到别家公司的邀请码了")
        _run(t())

    def test_normal_member_cannot_see_invite_code(self):
        async def t():
            from app.api.v1.organizations import get_my_invite_code
            async with self.Session() as db:
                alice = await db.get(User, 1)
                with self.assertRaises(HTTPException) as ctx:
                    await get_my_invite_code(alice, db)
                self.assertEqual(ctx.exception.status_code, 403)
        _run(t())

    def test_superadmin_can_see_own_org_code(self):
        async def t():
            from app.api.v1.organizations import create_organization, get_my_invite_code
            async with self.Session() as db:
                root = await db.get(User, 4)
                # 超管默认无组织 → 先给他一个，再验证能取到
                root.org_id = 2
                await db.commit()
                got = await get_my_invite_code(root, db)
                self.assertEqual(got.org_id, 2)
                _ = create_organization  # 仅引用，避免导入未使用告警
        _run(t())

    # ================================================== 重置邀请码

    def test_rotate_invite_invalidates_old_code(self):
        """重置后旧码立刻失效 —— 邀请码被误发到群里时的补救手段。"""
        async def t():
            from app.api.v1.auth import _resolve_org_for_register
            from app.api.v1.organizations import rotate_invite_code
            async with self.Session() as db:
                root = await db.get(User, 4)
                got = await rotate_invite_code(1, root, db)
                self.assertNotEqual(got.invite_code, "code-a-original")
                self.assertTrue(got.message)

                # 旧码：再也换不到组织（会 400）
                with self.assertRaises(HTTPException) as ctx:
                    await _resolve_org_for_register(db, "code-a-original")
                self.assertEqual(ctx.exception.status_code, 400)

                # 新码：可以正常加入 A 公司
                self.assertEqual(await _resolve_org_for_register(db, got.invite_code), 1)
        _run(t())

    # ================================================== 成员列表

    def test_superadmin_can_list_any_org_members(self):
        async def t():
            from app.api.v1.organizations import list_members
            async with self.Session() as db:
                root = await db.get(User, 4)
                ids_a = sorted(u.id for u in await list_members(1, root, db))
                ids_b = sorted(u.id for u in await list_members(2, root, db))
                self.assertEqual(ids_a, [1, 2])
                self.assertEqual(ids_b, [3])
        _run(t())

    def test_org_admin_can_list_own_members_only(self):
        """组织管理员看自己组织 → 可以；看别家组织 → 403。"""
        async def t():
            from app.api.v1.organizations import list_members
            async with self.Session() as db:
                admin_a = await db.get(User, 2)
                own = await list_members(1, admin_a, db)
                self.assertEqual(sorted(u.id for u in own), [1, 2])

                with self.assertRaises(HTTPException) as ctx:
                    await list_members(2, admin_a, db)
                self.assertEqual(ctx.exception.status_code, 403)
        _run(t())

    def test_normal_member_cannot_list_members(self):
        async def t():
            from app.api.v1.organizations import list_members
            async with self.Session() as db:
                alice = await db.get(User, 1)
                with self.assertRaises(HTTPException) as ctx:
                    await list_members(1, alice, db)
                self.assertEqual(ctx.exception.status_code, 403)
        _run(t())

    def test_member_list_does_not_leak_password_hash(self):
        """成员列表的**出口模型**里绝不能有密码哈希。

        注意这里验证的是"出口模型"而不是"接口返回的对象"：
        接口内部返回的是数据库对象（本身就带 password_hash），
        真正的过滤发生在 FastAPI 按 response_model 序列化那一步。
        所以要看住的是 UserOut 的字段清单 + 该接口确实声明了 response_model。
        """
        async def t():
            from app.api.v1.organizations import list_members
            from app.schemas.user import UserOut

            # 1) 出口模型字段清单里不能有任何密码相关的键
            fields = set(UserOut.model_fields)
            for bad in ("password", "password_hash", "hashed_password"):
                self.assertNotIn(bad, fields, f"对外用户模型里出现了敏感字段 {bad}")

            # 2) 真拿一个数据库对象套进出口模型，确认密码哈希被丢掉
            async with self.Session() as db:
                root = await db.get(User, 4)
                rows = await list_members(1, root, db)
                self.assertTrue(rows, "A公司应当有成员")
                dumped = UserOut.model_validate(rows[0]).model_dump()
                for bad in ("password", "password_hash"):
                    self.assertNotIn(bad, dumped)

            # 3) 该接口确实声明了 response_model（否则第 2 步的保证不成立）
            from app.main import app
            spec = app.openapi()
            resp = spec["paths"]["/api/v1/organizations/{org_id}/members"]["get"]["responses"]["200"]
            ref = str(resp.get("content", {}).get("application/json", {}).get("schema", {}))
            self.assertIn("UserOut", ref, "成员接口没有绑定 UserOut 出口模型，密码哈希可能被带出去")
        _run(t())

    # ================================================== 启停组织

    def test_update_org_can_deactivate(self):
        async def t():
            from app.api.v1.organizations import update_organization
            async with self.Session() as db:
                root = await db.get(User, 4)
                got = await update_organization(1, OrgUpdate(is_active=False), root, db)
                self.assertFalse(got.is_active)
                # 停用后：邀请码不再能用于注册（拒绝加入停用组织）
                from app.api.v1.auth import _resolve_org_for_register
                with self.assertRaises(HTTPException) as ctx:
                    await _resolve_org_for_register(db, "code-a-original")
                self.assertEqual(ctx.exception.status_code, 403)
        _run(t())

    def test_update_org_duplicate_name_rejected(self):
        async def t():
            from app.api.v1.organizations import update_organization
            async with self.Session() as db:
                root = await db.get(User, 4)
                with self.assertRaises(HTTPException) as ctx:
                    await update_organization(1, OrgUpdate(name="B公司"), root, db)
                self.assertEqual(ctx.exception.status_code, 409)
        _run(t())

    def test_update_missing_org_is_404(self):
        async def t():
            from app.api.v1.organizations import update_organization
            async with self.Session() as db:
                root = await db.get(User, 4)
                with self.assertRaises(HTTPException) as ctx:
                    await update_organization(9999, OrgUpdate(name="X"), root, db)
                self.assertEqual(ctx.exception.status_code, 404)
        _run(t())


if __name__ == "__main__":
    unittest.main()
