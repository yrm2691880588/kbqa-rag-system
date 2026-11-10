"""启动初始化：建表 + 轻量迁移（补列/调索引）+ 租户回填 + 首次创建管理员。

迁移策略（为什么不引入 Alembic）：
    本项目刻意保持"启动即自动升级"，不要求使用者会敲迁移命令 —— 目标用户是非技术用户，
    多一个必须手工执行的步骤就多一个卡住的地方。代价是迁移逻辑必须写得非常保守：
    全部用 IF NOT EXISTS / IF EXISTS 形式，任何时候重复执行都不报错（幂等）。
"""

import logging
import secrets

from sqlalchemy import inspect, select, text

# 导入所有模型，让它们注册到 Base.metadata（create_all 才会建这些表）
from app import models  # noqa: F401
from app.core.config import settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import AsyncSessionLocal, engine

logger = logging.getLogger("kbqa.init")

# 默认组织名：已有数据（升级前建的库和账号）全部归入这个组织
DEFAULT_ORG_NAME = "默认组织"


async def init_db(eng=None, session_factory=None) -> None:
    """应用启动时调用：确保表存在、补齐列与索引、回填租户归属、保证管理员存在。

    Args:
        eng: 数据库引擎；默认用全局 engine。
        session_factory: 会话工厂；默认用全局 AsyncSessionLocal。

    为什么要允许注入：
        迁移逻辑要动"已有客户的真实数据"，是整套代码里风险最高的部分。
        把连接对象做成可传入，就能在测试里造一个"升级前的老库"来完整演练一遍迁移，
        而不用碰真实数据库。没有这个口子，迁移就只能靠人肉在生产上试——那太危险了。
    """
    eng = eng if eng is not None else engine
    sf = session_factory if session_factory is not None else AsyncSessionLocal

    # 第一步：结构层面 —— 建缺失的表、给已有表补列
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_columns)          # users 表历史补列（phone / register_ip）
        await conn.run_sync(_ensure_tenancy_columns)  # 租户相关列 + 摘掉旧的全站唯一约束
        await conn.run_sync(_resync_sequences)        # 校准自增计数器（见函数注释）

    # 第二步：数据层面 —— 确保存在一个默认组织，并把"无归属"的老数据收进去
    default_org_id = await _ensure_default_org(sf)
    await _backfill_org(default_org_id, sf)

    # 第三步：回填完成后再建复合唯一索引（此时不会再出现 org_id 为空的行）
    async with eng.begin() as conn:
        await conn.run_sync(_ensure_tenancy_indexes)

    await _seed_admin(default_org_id, sf)
    logger.info(
        "数据库初始化完成（建表 + 列迁移 + 租户回填 + 管理员检查），默认组织 id=%s", default_org_id
    )


# --------------------------------------------------------------------------- 结构迁移


def _ensure_columns(sync_conn) -> None:
    """轻量迁移：为已有 users 表补上 phone / register_ip 列（仅当列不存在时执行）。

    说明：Base.metadata.create_all 只会创建"缺失的表"，不会给已有表加列。
    这里用 IF NOT EXISTS 保证幂等 —— 每次启动都执行，但只有第一次真正生效。
    """
    insp = inspect(sync_conn)
    existing_cols = {c["name"] for c in insp.get_columns("users")}
    if "phone" not in existing_cols:
        sync_conn.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR(20)"))
        logger.info("已为 users 表补充 phone 列（手机号登录支持）")
    if "register_ip" not in existing_cols:
        sync_conn.execute(text("ALTER TABLE users ADD COLUMN register_ip VARCHAR(64)"))
        logger.info("已为 users 表补充 register_ip 列（防批量注册）")
    # 手机号唯一索引（NULL 可重复，所以不影响未绑手机号的老用户）
    sync_conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_phone ON users (phone)"))
    sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_register_ip ON users (register_ip)"))


def _ensure_tenancy_columns(sync_conn) -> None:
    """轻量迁移：补租户相关列，并摘掉"知识库名全站唯一"这个旧约束。

    为什么必须摘掉旧约束：
        以前 knowledge_bases.name 是全站唯一的。加了组织之后，A 客户和 B 客户
        各自建一个叫「产品手册」的知识库是完全正常的，旧约束会把它拦掉。
    """
    insp = inspect(sync_conn)

    # --- users：所属组织 + 平台超管标记 ---
    user_cols = {c["name"] for c in insp.get_columns("users")}
    if "org_id" not in user_cols:
        sync_conn.execute(text("ALTER TABLE users ADD COLUMN org_id INTEGER"))
        logger.info("已为 users 表补充 org_id 列（所属组织）")
    if "is_superadmin" not in user_cols:
        # 默认 FALSE：已有账号一律先按普通账号处理，超管由 _seed_admin 显式指定
        sync_conn.execute(
            text("ALTER TABLE users ADD COLUMN is_superadmin BOOLEAN NOT NULL DEFAULT FALSE")
        )
        logger.info("已为 users 表补充 is_superadmin 列（平台超管标记）")
    sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_org_id ON users (org_id)"))

    # --- knowledge_bases：所属组织 ---
    kb_cols = {c["name"] for c in insp.get_columns("knowledge_bases")}
    if "org_id" not in kb_cols:
        sync_conn.execute(text("ALTER TABLE knowledge_bases ADD COLUMN org_id INTEGER"))
        logger.info("已为 knowledge_bases 表补充 org_id 列（所属组织）")
    sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_knowledge_bases_org_id ON knowledge_bases (org_id)"))

    _drop_global_unique_kb_name(sync_conn)


def _drop_global_unique_kb_name(sync_conn) -> None:
    """摘掉 knowledge_bases.name 的"全站唯一"约束（改为同组织内唯一）。

    PostgreSQL 与 SQLite 的处理方式不同，所以分开写：
    - PostgreSQL：唯一约束是一个独立的约束对象，可以直接 DROP CONSTRAINT；
    - SQLite：`UNIQUE(name)` 若写在建表语句里，是无法用 ALTER 摘掉的（SQLite 不支持）。
      只有"独立唯一索引"形式才能删。因此这里尽力而为，删不掉就给出明确提醒 ——
      残留的影响只是"不同组织不能有同名知识库"这一条限制，
      **不影响数据隔离（不会串数据）**，所以不阻断启动。
    """
    dialect = sync_conn.dialect.name

    if dialect == "postgresql":
        # 约束名由 PostgreSQL 自动生成，形如 knowledge_bases_name_key
        sync_conn.execute(text("ALTER TABLE knowledge_bases DROP CONSTRAINT IF EXISTS knowledge_bases_name_key"))
        sync_conn.execute(text("DROP INDEX IF EXISTS ix_knowledge_bases_name"))
        return

    if dialect == "sqlite":
        # SQLite：逐个候选名尝试删索引（独立索引才能删）
        for idx_name in ("ix_knowledge_bases_name", "knowledge_bases_name_key"):
            sync_conn.execute(text(f"DROP INDEX IF EXISTS {idx_name}"))
        # 检查是否还残留"覆盖 name 的唯一索引/约束"
        rows = sync_conn.execute(text("PRAGMA index_list('knowledge_bases')")).fetchall()
        still_global_unique = False
        for row in rows:
            # PRAGMA index_list 返回列：seq, name, unique, origin, partial
            idx_name, is_unique, origin = row[1], row[2], row[3]
            if not is_unique:
                continue
            cols = [r[2] for r in sync_conn.execute(text(f"PRAGMA index_info('{idx_name}')")).fetchall()]
            if cols == ["name"]:
                still_global_unique = True
        if still_global_unique:
            logger.warning(
                "SQLite 数据库中「知识库名全站唯一」是建表内联约束，无法在线摘除。"
                "影响：不同组织不能建同名知识库（仅是命名限制，不影响数据隔离）。"
                "如需彻底解除，请重建数据库或手工重建 knowledge_bases 表。"
            )
        return

    logger.info("数据库方言 %s：跳过 knowledge_bases 旧唯一约束清理（非 PostgreSQL/SQLite）", dialect)


def _ensure_tenancy_indexes(sync_conn) -> None:
    """回填完成后再建"同组织内知识库名唯一"的复合唯一索引。

    放在回填之后是因为：回填前所有行的 org_id 都是 NULL，此时建唯一索引没有意义。
    索引名与模型里 UniqueConstraint 的名字保持一致，PostgreSQL 下重复执行会自动跳过。
    """
    sync_conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_bases_org_name "
            "ON knowledge_bases (org_id, name)"
        )
    )


def _resync_sequences(sync_conn) -> None:
    """把每张表的自增计数器校准到"当前最大 id + 1"（仅 PostgreSQL）。

    为什么必须做（真实踩到的坑，不是理论风险）：
        PostgreSQL 的 SERIAL 自增计数器只在自己插入时推进。只要数据是**用 SQL 脚本
        导进库的**（从备份恢复、从别的环境迁移，都会显式带上 id），计数器就还停在 1。
        后果非常隐蔽：升级过程一切正常、数据也都在，但客户**建第一个新知识库/注册第一个
        新账号时**就报 "duplicate key value violates unique constraint ..._pkey"。
        从用户视角看就是"升级完之后系统坏了"，而且很难联想到是升级造成的。

    实现要点：
        ① 只处理"有 id 列且真的是自增"的表（pg_get_serial_sequence 返回空就跳过）；
        ② 用 GREATEST 保证**只调高、不调低** —— 绝不把计数器往回调，
           避免与已有 id 撞车。
    """
    if sync_conn.dialect.name != "postgresql":
        # SQLite / 其他库没有这个问题（它按 rowid 取最大值 +1），直接跳过
        return

    tables = [
        r[0]
        for r in sync_conn.execute(
            text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'id' "
                "AND data_type IN ('integer', 'bigint') "
                "ORDER BY table_name"
            )
        ).fetchall()
    ]

    fixed = 0
    for tbl in tables:
        seq = sync_conn.execute(
            text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": tbl}
        ).scalar()
        if not seq:
            continue  # 不是自增列（例如手工指定的 id），无需处理
        sync_conn.execute(
            text(
                f'SELECT setval(\'{seq}\', GREATEST('
                f'COALESCE((SELECT MAX(id) FROM "{tbl}"), 0) + 1, '
                f"(SELECT last_value FROM {seq})), false)"
            )
        )
        fixed += 1

    if fixed:
        logger.info("已校准 %d 张表的自增计数器（防止导库后新增记录主键冲突）", fixed)


# --------------------------------------------------------------------------- 数据回填


async def _ensure_default_org(session_factory) -> int:
    """确保存在一个默认组织，返回其 id。

    为什么需要它：
        从旧版本升级上来的数据库里，老的知识库和账号都没有"归属"。
        直接留空会导致这些数据谁都看不见（等于数据丢失）。
        所以统一把它们收进一个"默认组织"，保证升级后老用户照常能用。
    """
    async with session_factory() as session:
        existing = (await session.execute(select(models.Organization).order_by(models.Organization.id))).scalars().first()
        if existing is not None:
            return existing.id

        # 邀请码用随机串：不放可猜的规律值，避免有人拿默认码混进组织
        org = models.Organization(
            name=DEFAULT_ORG_NAME,
            description="系统自动创建：升级前的历史数据（知识库与账号）都归入此组织。",
            invite_code=secrets.token_hex(8),
        )
        session.add(org)
        await session.commit()
        await session.refresh(org)
        logger.info("已创建默认组织「%s」（id=%s）用于承接历史数据", org.name, org.id)
        return org.id


async def _backfill_org(default_org_id: int, session_factory) -> None:
    """把没有归属的历史数据收进默认组织（幂等：只处理 org_id 为空的行）。"""
    from sqlalchemy import func, update

    async with session_factory() as session:
        # 1) 账号
        r1 = await session.execute(
            update(models.User)
            .where(models.User.org_id.is_(None))
            .values(org_id=default_org_id)
        )
        # 2) 知识库
        r2 = await session.execute(
            update(models.KnowledgeBase)
            .where(models.KnowledgeBase.org_id.is_(None))
            .values(org_id=default_org_id)
        )
        await session.commit()

        n_user, n_kb = r1.rowcount or 0, r2.rowcount or 0
        if n_user or n_kb:
            logger.info("租户回填完成：%d 个账号、%d 个知识库已归入默认组织", n_user, n_kb)

        # 顺带做一次一致性体检：还有多少"无归属"的行（正常情况下应为 0）
        left_user = await session.scalar(
            select(func.count()).select_from(models.User).where(models.User.org_id.is_(None))
        )
        left_kb = await session.scalar(
            select(func.count()).select_from(models.KnowledgeBase).where(models.KnowledgeBase.org_id.is_(None))
        )
        if left_kb:
            logger.warning("仍有 %d 个知识库没有归属组织，这些库当前对所有人不可见", left_kb)
        if left_user:
            logger.info("仍有 %d 个账号未归属组织（平台超管属正常情况）", left_user)


async def _seed_admin(default_org_id: int, session_factory) -> None:
    """确保管理员账号存在，并且是平台超管。

    为什么要显式提升为超管：
        升级到多租户版本后，"谁能创建组织"必须有明确答案。
        如果老管理员不是超管，就会出现"没人能建组织、系统锁死"的局面。
        所以这里保证 .env 里配置的管理员账号一定具备超管身份。
    """
    async with session_factory() as session:
        admin = (
            await session.execute(
                select(models.User).where(models.User.username == settings.admin_username)
            )
        ).scalar_one_or_none()

        if admin is None:
            admin = models.User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                nickname="系统管理员",
                role="admin",
                is_superadmin=True,
                org_id=default_org_id,
            )
            session.add(admin)
            await session.commit()
            logger.info("已创建初始管理员账号：%s（平台超管）", settings.admin_username)
            return

        # 已存在：补齐超管身份与组织归属（幂等，重复启动不会反复写库）
        changed = False
        if not admin.is_superadmin:
            admin.is_superadmin = True
            changed = True
        if admin.org_id is None:
            admin.org_id = default_org_id
            changed = True
        if admin.role != "admin":
            admin.role = "admin"
            changed = True
        if changed:
            session.add(admin)
            await session.commit()
            logger.info("已将管理员 %s 提升为平台超管并纳入默认组织", admin.username)
