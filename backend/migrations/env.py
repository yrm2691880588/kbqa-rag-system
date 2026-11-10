"""Alembic 运行环境（P2-18）：告诉 alembic "数据库在哪、表结构长什么样"。

这里有三件事必须做对：

  ① **连接串优先用调用方给的，没有才从项目配置读**。
     写死成"永远从项目配置读"会带来两个麻烦：
       · 测试没法把迁移跑在临时库上（会去动正式库，很危险）；
       · 运维想临时把迁移跑在另一台库上也没有办法。
     所以顺序是：ini/命令行里给了就用它 → 没给才回落到项目的 DATABASE_URL。

  ② **用异步引擎跑迁移**（复用项目已有的 asyncpg 驱动）。
     为什么要提这个：很多教程会让你把 `+asyncpg` 换成 `+psycopg` 走同步驱动，
     但那样要**多装一个数据库驱动**。项目里已经装了 asyncpg，
     复用它能少一个依赖、少一处版本冲突的可能（客户服务器上少装一个东西就少一个坑）。

  ③ **把项目里所有模型都 import 一遍**。
     自动生成迁移时，alembic 靠"模型定义"和"数据库现状"做对比来推导差异。
     如果某个模型没被 import，alembic 就以为它不存在 ——
     会生成一条"删掉这张表"的迁移。这不是吓唬人：**漏 import 会生成删表语句**，
     跑起来就把数据删了。所以这里显式 import app.models（它内部会 import 全部模型）。
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# 让 alembic 能找到 app 包（它默认在 backend 目录下运行）
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.core.config import settings          # noqa: E402
import app.models                            # noqa: E402,F401  必须 import：见文件头 ③
from app.db.base import Base                 # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 自动生成迁移时要对比的目标：项目里所有模型定义的总和
target_metadata = Base.metadata


def _database_url() -> str:
    """取数据库连接串：优先用外部指定的，没有才回落到项目配置。"""
    given = (config.get_main_option("sqlalchemy.url") or "").strip()
    if given:
        return given
    return (settings.database_url or "").strip()


def run_migrations_offline() -> None:
    """离线模式：不连数据库，只把 SQL 打印出来（给 DBA 审阅用）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # compare_type：能看出"字段类型改了"（比如 32 位短文本改成 255 位长文本）
        compare_type=True,
        # render_as_batch：SQLite 不支持直接改列，打开它 alembic 会用
        # "建新表→搬数据→换名字"的方式绕过（这样本地开发用 SQLite 也能跑迁移）
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    config.set_main_option("sqlalchemy.url", _database_url())
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式：真的连上数据库执行迁移。"""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

