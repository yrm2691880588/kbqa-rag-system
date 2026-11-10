"""「老库升级」迁移测试 —— P1-A 多租户改造里风险最高的一步。

这个文件在测什么（大白话）：
    客户已经在用旧版本了，数据库里存着真实的知识库和账号。
    我们这次给系统加了"组织"概念，需要在**不丢数据**的前提下把老库升上来：
      ① 给表补上 org_id（归属组织）和 is_superadmin（超管标记）两列；
      ② 把"没人认领"的老数据统一收进一个「默认组织」，否则老用户升级后会发现
         自己的知识库全"消失"了（实际是被隔离规则挡住）——这在客户眼里等同于数据丢失；
      ③ 把管理员提升为平台超管，否则升级后没人能建组织，系统直接锁死；
      ④ 全程可重复执行（幂等），客户重启多少次都不会重复建组织、重复写数据。

为什么必须写这组测试：
    迁移要动客户的真实数据，出错代价极高。所以这里刻意**在现场造一个"升级前的老库"**
    （用旧版本的建表语句 + 老的唯一约束），把 init_db 在这上面完整跑一遍，
    用数字验证它真的做对了，而不是"看起来对"。

运行方式（backend 目录下）：
    python -m unittest tests.test_migration_tenancy -v
"""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db import init_db as init_db_module
from app.models.knowledge_base import KnowledgeBase
from app.models.organization import Organization
from app.models.user import User

# ---------------------------------------------------------------------------
# 旧版本（升级前）的建表语句 —— 故意不含 org_id / is_superadmin
# ---------------------------------------------------------------------------
# 注意 knowledge_bases 这里用**独立的唯一索引**，而不是内联 UNIQUE：
# 旧模型写的是 name = mapped_column(unique=True, index=True)，
# SQLAlchemy 在这种情况下生成的正是名为 ix_knowledge_bases_name 的独立唯一索引
# （PostgreSQL 上则可能是一个名为 knowledge_bases_name_key 的约束）。
# 只有这种形态的约束才能被 ALTER 摘掉，所以这是最典型、也最值得验证的一种老库。
LEGACY_SCHEMA_DROPPABLE = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(32) NOT NULL,
    phone VARCHAR(20),
    password_hash VARCHAR(255) NOT NULL,
    nickname VARCHAR(64) NOT NULL DEFAULT '',
    role VARCHAR(16) NOT NULL DEFAULT 'user',
    register_ip VARCHAR(64),
    is_active BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_login_at DATETIME
);
CREATE UNIQUE INDEX ix_users_username ON users (username);

CREATE TABLE knowledge_bases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    created_by INTEGER,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    doc_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX ix_knowledge_bases_name ON knowledge_bases (name);
"""

# 另一种老库形态：唯一约束写死在建表语句里（SQLite 无法在线摘除）。
# 这是最坏情况，用来验证"摘不掉也不能把升级搞崩、更不能影响数据隔离"。
LEGACY_SCHEMA_INLINE = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(32) NOT NULL,
    phone VARCHAR(20),
    password_hash VARCHAR(255) NOT NULL,
    nickname VARCHAR(64) NOT NULL DEFAULT '',
    role VARCHAR(16) NOT NULL DEFAULT 'user',
    register_ip VARCHAR(64),
    is_active BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_login_at DATETIME
);
CREATE TABLE knowledge_bases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    created_by INTEGER,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    doc_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _run(coro):
    return asyncio.run(coro)


class MigrationTestCase(unittest.TestCase):
    """公共脚手架：造一个"升级前的老库"，供各条测试复用。"""

    LEGACY_SCHEMA = LEGACY_SCHEMA_DROPPABLE

    def setUp(self):
        # 用临时文件而不是内存库：迁移过程里 init_db 会开多个连接/事务，
        # 文件库更接近真实生产环境，避免"内存库恰好能过"的假绿。
        fd, self.db_path = tempfile.mkstemp(suffix=".db", prefix="kbqa_legacy_")
        os.close(fd)
        self.url = f"sqlite+aiosqlite:///{self.db_path}"
        _run(self._build_legacy_db())

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    async def _build_legacy_db(self):
        """按旧版本结构建库并塞入历史数据（模拟客户正在用的库）。"""
        eng = create_async_engine(self.url)
        async with eng.begin() as conn:
            for stmt in self.LEGACY_SCHEMA.strip().split(";"):
                if stmt.strip():
                    await conn.execute(text(stmt))
            # 历史账号：管理员 + 一个普通成员
            await conn.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, nickname, role) VALUES "
                    "(:i, :u, :p, :n, :r)"
                ),
                [
                    {"i": 1, "u": settings.admin_username, "p": "legacy-hash", "n": "老管理员", "r": "user"},
                    {"i": 2, "u": "laoyonghu", "p": "legacy-hash", "n": "老用户", "r": "user"},
                ],
            )
            # 历史知识库
            await conn.execute(
                text("INSERT INTO knowledge_bases (id, name, description) VALUES (:i, :n, :d)"),
                [
                    {"i": 1, "n": "产品手册", "d": "升级前就存在的库"},
                    {"i": 2, "n": "内部制度", "d": "升级前就存在的库"},
                ],
            )
        await eng.dispose()

    def _make_engine(self):
        eng = create_async_engine(self.url)
        sf = async_sessionmaker(eng, expire_on_commit=False)
        return eng, sf

    async def _migrate(self, times: int = 1):
        """跑迁移，返回 (engine, session_factory)。跑多次用于验证幂等。"""
        eng, sf = self._make_engine()
        for _ in range(times):
            await init_db_module.init_db(eng=eng, session_factory=sf)
        return eng, sf


class TestLegacyUpgrade(MigrationTestCase):
    """迁移主流程：列补齐 + 数据回填 + 管理员提升。"""

    def test_tenancy_columns_are_added(self):
        """① 结构：两列都要补上，否则隔离体系无从谈起。"""
        async def t():
            eng, sf = await self._migrate()
            async with eng.connect() as conn:
                user_cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(users)"))).fetchall()}
                kb_cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(knowledge_bases)"))).fetchall()}
            await eng.dispose()

            self.assertIn("org_id", user_cols, "users 表缺少 org_id（所属组织）")
            self.assertIn("is_superadmin", user_cols, "users 表缺少 is_superadmin（超管标记）")
            self.assertIn("org_id", kb_cols, "knowledge_bases 表缺少 org_id（所属组织）")
        _run(t())

    def test_legacy_data_is_backfilled_into_default_org(self):
        """② 数据：历史知识库和账号必须全部归入默认组织，一个都不许漏。

        漏掉的后果 = 老用户升级后看不到自己的知识库（在客户看来就是数据丢了）。
        """
        async def t():
            eng, sf = await self._migrate()
            async with sf() as db:
                from sqlalchemy import func, select
                org = (await db.execute(select(Organization))).scalars().first()
                self.assertIsNotNone(org, "迁移后应当存在一个默认组织")

                kbs = (await db.execute(select(KnowledgeBase))).scalars().all()
                self.assertEqual(len(kbs), 2, "历史知识库数量不应变化")
                for kb in kbs:
                    self.assertEqual(kb.org_id, org.id, f"知识库「{kb.name}」没有归入默认组织")

                users = (await db.execute(select(User))).scalars().all()
                self.assertEqual(len(users), 2, "历史账号数量不应变化")
                for u in users:
                    self.assertEqual(u.org_id, org.id, f"账号 {u.username} 没有归入默认组织")

                # 反向对照：确认真的没有"无归属"的残留行
                left_kb = await db.scalar(
                    select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.org_id.is_(None))
                )
                self.assertEqual(left_kb, 0, "迁移后仍有知识库没有归属组织")
            await eng.dispose()
        _run(t())

    def test_default_org_uses_random_invite_code(self):
        """默认组织的邀请码必须是随机串，不能是可猜的固定值。"""
        async def t():
            eng, sf = await self._migrate()
            async with sf() as db:
                from sqlalchemy import select
                org = (await db.execute(select(Organization))).scalars().first()
                code = org.invite_code
            await eng.dispose()

            self.assertTrue(code, "邀请码不能为空")
            self.assertGreaterEqual(len(code), 12, "邀请码太短，容易被猜到")
            self.assertNotIn(code.lower(), ("default", "123456", "000000"), "不能是明显可猜的值")
            self.assertNotEqual(code, org.name, "邀请码不能等于组织名")
        _run(t())

    def test_legacy_admin_is_promoted_to_superadmin(self):
        """③ 管理员提升：否则升级后没人能建组织，系统锁死。"""
        async def t():
            eng, sf = await self._migrate()
            async with sf() as db:
                from sqlalchemy import select
                admin = (
                    await db.execute(select(User).where(User.username == settings.admin_username))
                ).scalar_one()
                other = (
                    await db.execute(select(User).where(User.username == "laoyonghu"))
                ).scalar_one()
                admin_flag, admin_role, admin_org = admin.is_superadmin, admin.role, admin.org_id
                other_flag = other.is_superadmin
            await eng.dispose()

            self.assertTrue(admin_flag, "配置里的管理员必须是平台超管")
            self.assertEqual(admin_role, "admin", "管理员角色应为 admin")
            self.assertIsNotNone(admin_org, "管理员也应当有组织归属")
            # 反向对照：普通老用户绝不能顺手被提成超管
            self.assertFalse(other_flag, "普通老用户被错误提升为超管（越权风险）")
        _run(t())

    def test_old_global_unique_kb_name_is_dropped(self):
        """④ 旧的"知识库名全站唯一"必须摘掉，否则不同客户不能各建一个「产品手册」。"""
        async def t():
            eng, sf = await self._migrate()
            async with sf() as db:
                from sqlalchemy import select
                org = (await db.execute(select(Organization))).scalars().first()
                # 迁移后：在另一个组织里建一个同名知识库，应当成功
                db.add(Organization(id=99, name="新客户", invite_code="code-new"))
                await db.flush()
                db.add(KnowledgeBase(name="产品手册", org_id=99))
                await db.commit()

                same = (await db.execute(
                    select(KnowledgeBase).where(KnowledgeBase.name == "产品手册")
                )).scalars().all()
                self.assertEqual(len(same), 2, "不同组织应当允许有同名知识库（旧约束未被摘掉）")
                self.assertNotEqual(same[0].org_id, same[1].org_id, "两个同名库应分属不同组织")
                _ = org
            await eng.dispose()
        _run(t())

    def test_composite_unique_index_is_created(self):
        """⑤ 回填完成后要建好"(组织, 名称)"复合唯一索引。"""
        async def t():
            eng, sf = await self._migrate()
            async with eng.connect() as conn:
                names = {r[1] for r in (await conn.execute(text("PRAGMA index_list('knowledge_bases')"))).fetchall()}
            await eng.dispose()
            self.assertIn(
                "uq_knowledge_bases_org_name", names,
                "缺少 (org_id, name) 复合唯一索引：同组织内可能出现重名知识库",
            )
        _run(t())

    def test_migration_is_idempotent(self):
        """⑥ 幂等：重复启动不能重复建组织、重复改数据。

        init_db 每次应用启动都会跑，跑三遍必须和跑一遍的结果完全一样。
        """
        async def t():
            eng, sf = await self._migrate(times=3)
            async with sf() as db:
                from sqlalchemy import func, select
                n_org = await db.scalar(select(func.count()).select_from(Organization))
                n_user = await db.scalar(select(func.count()).select_from(User))
                n_kb = await db.scalar(select(func.count()).select_from(KnowledgeBase))
                orphan = await db.scalar(
                    select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.org_id.is_(None))
                )
            await eng.dispose()

            self.assertEqual(n_org, 1, "重复执行迁移创建了多个默认组织")
            self.assertEqual(n_user, 2, "重复执行迁移改动了账号数量")
            self.assertEqual(n_kb, 2, "重复执行迁移改动了知识库数量")
            self.assertEqual(orphan, 0, "重复执行迁移后出现无归属知识库")
        _run(t())

    def test_backfill_does_not_touch_already_owned_rows(self):
        """⑦ 幂等细化：已经归属某组织的数据，不能被回填逻辑"抢"回默认组织。

        否则第二次启动会把客户已经分好的数据全打乱。
        """
        async def t():
            eng, sf = self._make_engine()
            await init_db_module.init_db(eng=eng, session_factory=sf)

            # 迁移后手工做一个"客户 B"的组织，把其中一个库划过去
            async with sf() as db:
                from sqlalchemy import select
                db.add(Organization(id=50, name="客户B", invite_code="code-b"))
                await db.flush()
                kb = (await db.execute(select(KnowledgeBase).where(KnowledgeBase.name == "内部制度"))).scalar_one()
                kb.org_id = 50
                await db.commit()

            # 再次启动（模拟客户重启服务）
            await init_db_module.init_db(eng=eng, session_factory=sf)

            async with sf() as db:
                from sqlalchemy import select
                kb = (await db.execute(select(KnowledgeBase).where(KnowledgeBase.name == "内部制度"))).scalar_one()
                got = kb.org_id
            await eng.dispose()

            self.assertEqual(got, 50, "已被划归客户B的知识库被回填逻辑抢回默认组织（会打乱客户数据）")
        _run(t())


class TestLegacyUpgradeWithInlineConstraint(MigrationTestCase):
    """最坏情况：老库把唯一约束写死在建表语句里（SQLite 无法在线摘除）。

    这种情况下代码会打一条警告然后继续跑。这里要验证两件事：
      ① 升级不能因此崩掉；
      ② 数据隔离必须完好无损（残留的只是"不能同名"这条命名限制，不串数据）。
    """

    LEGACY_SCHEMA = LEGACY_SCHEMA_INLINE

    def test_migration_survives_inline_unique(self):
        async def t():
            eng, sf = await self._migrate()
            async with sf() as db:
                from sqlalchemy import func, select
                n_org = await db.scalar(select(func.count()).select_from(Organization))
                orphan = await db.scalar(
                    select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.org_id.is_(None))
                )
                admin = (
                    await db.execute(select(User).where(User.username == settings.admin_username))
                ).scalar_one()
                flag = admin.is_superadmin
            await eng.dispose()

            self.assertEqual(n_org, 1, "默认组织应当仍然被创建")
            self.assertEqual(orphan, 0, "历史知识库仍然必须被回填")
            self.assertTrue(flag, "管理员仍然必须被提升为超管")
        _run(t())

    def test_isolation_still_holds_after_worst_case_migration(self):
        """最坏情况下隔离必须仍然成立：跨组织互相看不到。"""
        async def t():
            eng, sf = await self._migrate()
            async with sf() as db:
                from sqlalchemy import select
                from app.core import tenancy
                # 再建一个组织 + 一个成员，验证跨组织不可见
                db.add(Organization(id=77, name="另一家", invite_code="code-other"))
                await db.flush()
                bob = User(username="bob", password_hash="x", role="user", org_id=77)
                db.add(bob)
                await db.commit()
                await db.refresh(bob)

                visible = await tenancy.accessible_kb_ids(db, bob)
                # 老库里那两个知识库属于默认组织，bob 一个都不该看到
                self.assertEqual(visible, [], f"越权可见：bob 不应看到任何老库的知识库，实得 {visible}")
            await eng.dispose()
        _run(t())


if __name__ == "__main__":
    unittest.main()
