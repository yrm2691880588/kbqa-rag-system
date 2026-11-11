"""多租户隔离测试（P1 第 7 项的核心验收）。

这个文件在测什么（大白话）：
    "A 公司的资料，B 公司的人能不能看到？" —— 答案必须是一个都不能看到。
    所以这里刻意构造两个组织、各自一个知识库，然后从各个可能的入口尝试越权访问。

为什么要专门写这一组测试：
    隔离这种事**不能靠肉眼审查代码来保证**。漏掉任何一处判断（本项目就真实漏过两处：
    文档接口按 id 取文档没校验归属、空 kb_ids 被当成"不限制"），
    后果都是客户数据泄露。所以每个入口都要有一条"越权必须失败"的测试钉住。

运行方式（backend 目录下）：
    python -m unittest tests.test_tenancy -v
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

from app.core import tenancy
from app.db.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.organization import Organization
from app.models.user import User


async def _fake_embed(texts):
    """假的向量化结果：避免测试真的去调百炼接口。"""
    return [[0.1, 0.2, 0.3]]


def _run(coro):
    return asyncio.run(coro)


class TenancyTestCase(unittest.TestCase):
    """公共脚手架：建两个组织，各一个知识库，各一个成员。"""

    def setUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        _run(self._seed())

    async def _seed(self):
        """准备数据：
        A 组织(1)：知识库 kb_a(1)、成员 alice(id=1)
        B 组织(2)：知识库 kb_b(2)、成员 bob(id=2)
        另有一个平台超管 root(id=3)
        """
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self.Session() as db:
            db.add_all([
                Organization(id=1, name="A公司", invite_code="code-aaa"),
                Organization(id=2, name="B公司", invite_code="code-bbb"),
            ])
            await db.flush()
            db.add_all([
                User(id=1, username="alice", password_hash="x", role="user", org_id=1),
                User(id=2, username="bob", password_hash="x", role="user", org_id=2),
                User(id=3, username="root", password_hash="x", role="admin",
                     org_id=None, is_superadmin=True),
                # 故意造一个"没有归属组织"的账号，验证它什么也看不到
                User(id=4, username="homeless", password_hash="x", role="user", org_id=None),
            ])
            await db.flush()
            db.add_all([
                KnowledgeBase(id=1, name="产品手册", org_id=1),
                KnowledgeBase(id=2, name="产品手册", org_id=2),  # 故意同名：不同组织允许
            ])
            await db.commit()

    # ---------------------------------------------------------------- 基础查询

    def test_two_orgs_can_have_same_kb_name(self):
        """不同组织可以有同名知识库（旧版本因为"全站唯一"是做不到的）。"""
        async def t():
            async with self.Session() as db:
                rows = (await db.execute(
                    select(KnowledgeBase).where(KnowledgeBase.name == "产品手册")
                )).scalars().all()
                self.assertEqual(len(rows), 2, "两个组织各有一个同名库，应当都存在")
        _run(t())

    def test_accessibility_is_org_scoped(self):
        """成员只能看到本组织的知识库 id。"""
        async def t():
            async with self.Session() as db:
                alice = await db.get(User, 1)
                bob = await db.get(User, 2)
                self.assertEqual(sorted(await tenancy.accessible_kb_ids(db, alice)), [1])
                self.assertEqual(sorted(await tenancy.accessible_kb_ids(db, bob)), [2])
        _run(t())

    def test_superadmin_sees_all_kbs(self):
        """平台超管不受组织限制。"""
        async def t():
            async with self.Session() as db:
                root = await db.get(User, 3)
                self.assertEqual(sorted(await tenancy.accessible_kb_ids(db, root)), [1, 2])
        _run(t())

    def test_user_without_org_sees_nothing(self):
        """未归属任何组织的账号：一个库都看不到（绝不能是"看到全部"）。"""
        async def t():
            async with self.Session() as db:
                u = await db.get(User, 4)
                self.assertEqual(await tenancy.accessible_kb_ids(db, u), [])
        _run(t())

    # ---------------------------------------------------------------- 范围收敛

    def test_member_default_scope_is_own_org(self):
        """不指定库 → 收敛为本组织的库；且**返回空列表而不是 None**。"""
        async def t():
            async with self.Session() as db:
                alice = await db.get(User, 1)
                self.assertEqual(await tenancy.resolve_kb_scope(db, alice, None), [1])
        _run(t())

    def test_member_requesting_foreign_kb_is_dropped(self):
        """A 公司成员点名要 B 公司的库 → 越权部分被剔除，拿不到任何结果。"""
        async def t():
            async with self.Session() as db:
                alice = await db.get(User, 1)
                # 只要别家的库
                self.assertEqual(await tenancy.resolve_kb_scope(db, alice, [2]), [])
                # 混着要：自己的留下，别家的剔除
                self.assertEqual(await tenancy.resolve_kb_scope(db, alice, [1, 2]), [1])
        _run(t())

    def test_scope_never_returns_none_for_normal_user(self):
        """最重要的一条：普通用户的检索范围**永远不能是 None**。

        None 在检索链路里代表"不限制"，一旦普通用户拿到 None，
        他就会搜到全站所有客户的知识库。这里把这条语义钉死。
        """
        async def t():
            async with self.Session() as db:
                for uid in (1, 2, 4):  # 有库的、有库的、没归属的
                    u = await db.get(User, uid)
                    got = await tenancy.resolve_kb_scope(db, u, None)
                    self.assertIsNotNone(got, f"用户 {u.username} 的范围不能是 None")
                    self.assertIsInstance(got, list)
                    # 点名要不存在的库，同样不能退化成 None
                    self.assertIsNotNone(await tenancy.resolve_kb_scope(db, u, [999]))
        _run(t())

    def test_superadmin_scope_keeps_none(self):
        """超管不指定时保持 None（= 不限制），这是唯一允许出现 None 的角色。"""
        async def t():
            async with self.Session() as db:
                root = await db.get(User, 3)
                self.assertIsNone(await tenancy.resolve_kb_scope(db, root, None))
                self.assertEqual(await tenancy.resolve_kb_scope(db, root, [2]), [2])
        _run(t())

    # ---------------------------------------------------------------- 取单个库

    def test_get_scoped_kb_rejects_foreign(self):
        """取别家组织的知识库 → 404（不是 403，避免泄露"存在性"）。"""
        async def t():
            async with self.Session() as db:
                alice = await db.get(User, 1)
                with self.assertRaises(HTTPException) as ctx:
                    await tenancy.get_scoped_kb(db, alice, 2)
                self.assertEqual(ctx.exception.status_code, 404)
        _run(t())

    def test_get_scoped_kb_allows_own(self):
        """取自己的知识库 → 正常返回。"""
        async def t():
            async with self.Session() as db:
                alice = await db.get(User, 1)
                kb = await tenancy.get_scoped_kb(db, alice, 1)
                self.assertEqual(kb.id, 1)
        _run(t())

    def test_get_scoped_kb_superadmin_can_access_any(self):
        """平台超管可以取任意组织的库（运维排查需要）。"""
        async def t():
            async with self.Session() as db:
                root = await db.get(User, 3)
                self.assertEqual((await tenancy.get_scoped_kb(db, root, 2)).id, 2)
        _run(t())

    def test_get_scoped_kb_missing_is_404(self):
        """不存在的库 → 404。"""
        async def t():
            async with self.Session() as db:
                alice = await db.get(User, 1)
                with self.assertRaises(HTTPException) as ctx:
                    await tenancy.get_scoped_kb(db, alice, 99999)
                self.assertEqual(ctx.exception.status_code, 404)
        _run(t())


class TestEmptyScopeIsNotUnfiltered(unittest.TestCase):
    """回归测试：空的知识库列表**绝不能**被当成"不加限制"。

    真实踩过的坑：检索函数里写的是 `if kb_ids:`，空列表是假值，
    于是过滤条件根本没加上，等于搜遍所有客户的知识库。属于严重数据泄露。
    """

    def test_empty_kb_ids_short_circuits(self):
        """kb_ids=[] 必须直接返回空结果，且不去碰向量库。"""
        async def t():
            from app.services import vector_store
            # 传一个空列表：应当在调用向量库之前就返回空
            hits = await vector_store.search([0.1, 0.2, 0.3], kb_ids=[])
            self.assertEqual(hits, [])
        _run(t())

    def test_none_means_unrestricted_still_supported(self):
        """kb_ids=None 仍表示"不限制"（平台级调用需要这个语义），但会真的去查向量库。

        ⚠️ 这个用例以前写得很弱：用 `except Exception: return` 兜底，
        结果任何异常（包括参数拼错、导入失败）都算通过 ——
        等于什么都没验证。现在改为**直接观察向量库客户端有没有被调用**：
        把底层 search 换成探针，看它是否真的被调用、收到的 kb_ids 是什么。
        """
        async def t():
            from unittest import mock

            from app.services import vector_store

            called = {}

            async def fake_search(**kwargs):
                # 记录调用参数，用于断言"没被提前短路成空列表"
                called.update(kwargs)
                return []

            # 探针替换掉真正会连 Qdrant 的那一层
            with mock.patch.object(vector_store, "search", side_effect=fake_search):
                # 这里测的是 retrieve 层对 kb_ids=None 的传递语义；
                # 若 retrieve 内部直接短路，就不会调到 fake_search
                from app.services import retriever

                with mock.patch.object(retriever, "embed_texts", _fake_embed):
                    try:
                        await retriever.retrieve("测试问题", kb_ids=None)
                    except TypeError:
                        # retrieve 的签名若变化，这里会明确失败而不是被吞掉
                        raise

            # 关键断言：确实走到了向量库那一层，且 kb_ids 原样传的是 None
            self.assertTrue(called, "kb_ids=None 被提前短路了，没有去查向量库")
            self.assertIsNone(
                called.get("kb_ids"),
                f"kb_ids=None 应原样传给向量库（表示不限制），实际收到 {called.get('kb_ids')!r}",
            )
        _run(t())


class TestInviteCodeRegistration(unittest.TestCase):
    """邀请码注册：决定新账号进哪个组织。"""

    def setUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        _run(self._seed())

    async def _seed(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.Session() as db:
            db.add_all([
                Organization(id=1, name="默认组织", invite_code="code-default"),
                Organization(id=2, name="某客户", invite_code="code-customer"),
            ])
            await db.commit()

    def test_valid_code_joins_that_org(self):
        async def t():
            from app.api.v1.auth import _resolve_org_for_register
            async with self.Session() as db:
                self.assertEqual(await _resolve_org_for_register(db, "code-customer"), 2)
        _run(t())

    def test_invalid_code_rejected(self):
        """填了码但码不对 → 明确拒绝，绝不能"码错就默默放进默认组织"。"""
        async def t():
            from app.api.v1.auth import _resolve_org_for_register
            async with self.Session() as db:
                with self.assertRaises(HTTPException) as ctx:
                    await _resolve_org_for_register(db, "猜一个看看")
                self.assertEqual(ctx.exception.status_code, 400)
        _run(t())

    def test_no_code_falls_back_to_default_org(self):
        """不填码（私有部署场景）→ 加入默认组织，保证注册后能正常工作。"""
        async def t():
            from app.api.v1.auth import _resolve_org_for_register
            async with self.Session() as db:
                self.assertEqual(await _resolve_org_for_register(db, ""), 1)
        _run(t())

    def test_no_code_rejected_when_invite_required(self):
        """开启了"必须凭码注册"（对外 SaaS）→ 不填码必须被拒绝。

        这是防止陌生人自助注册进客户组织的关键开关，必须单独钉住。
        """
        async def t():
            from unittest.mock import patch
            from app.api.v1.auth import _resolve_org_for_register
            from app.core.config import settings
            async with self.Session() as db:
                with patch.object(settings, "registration_requires_invite", True):
                    with self.assertRaises(HTTPException) as ctx:
                        await _resolve_org_for_register(db, "")
                    self.assertEqual(ctx.exception.status_code, 400)
        _run(t())


class TestRegisterPolicyEndpoint(unittest.TestCase):
    """公开的注册策略接口：只暴露"要不要邀请码"这一个布尔值。

    前端靠它在注册页把邀请码标成"选填"还是"必填"，所以必须真实反映配置。
    """

    def test_policy_follows_config_false(self):
        async def t():
            from unittest.mock import patch
            from app.api.v1.auth import get_register_policy
            from app.core.config import settings
            with patch.object(settings, "registration_requires_invite", False):
                self.assertFalse((await get_register_policy()).invite_required)
        _run(t())

    def test_policy_follows_config_true(self):
        async def t():
            from unittest.mock import patch
            from app.api.v1.auth import get_register_policy
            from app.core.config import settings
            with patch.object(settings, "registration_requires_invite", True):
                self.assertTrue((await get_register_policy()).invite_required)
        _run(t())

    def test_policy_leaks_no_org_info(self):
        """返回体里只能有 invite_required 一个字段 —— 不能顺手把组织名/邀请码带出去。"""
        async def t():
            from app.api.v1.auth import get_register_policy
            got = await get_register_policy()
            self.assertEqual(set(got.model_dump().keys()), {"invite_required"})
        _run(t())


if __name__ == "__main__":
    unittest.main()
