"""数据库迁移工具化测试（P2-18）。

这个文件在测什么（大白话）：
    引入 alembic（数据库搬家工具）的真正价值不是"能执行 SQL"，
    而是**能发现"改了模型却忘了写迁移"**。

    这个漏法非常隐蔽：开发时改了模型定义（比如给用户加个字段），
    自己的本机数据库是全新建的、天然带着新字段，所以一切正常；
    但客户那台服务器上跑的是老库 —— 升级后代码去读那个不存在的字段，
    直接报错。而且这种错只在别人的环境里出现，本地怎么测都没事。

    所以这里做一条"体检"：把迁移脚本跑出来的表结构，
    和代码里模型定义的表结构**逐项对比**，有任何差异就报红。
    只要有人改了模型没写迁移，这条测试立刻拦下来。

运行方式（backend 目录下）：
    python -m unittest tests.test_migrations -v
"""

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.db.base import Base
import app.models  # noqa: F401  必须 import，否则模型不全、对比结果没意义

BACKEND = pathlib.Path(__file__).resolve().parent.parent


def _alembic_config(db_path: pathlib.Path) -> Config:
    """造一个指向临时库的 alembic 配置。

    这里用的连接串是**异步驱动的 SQLite**（`sqlite+aiosqlite`），
    因为项目的 env.py 走的是异步引擎（复用项目已有的驱动，不多装一个）。
    """
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


class MigrationChainTestCase(unittest.TestCase):
    """迁移脚本本身的基本体检。"""

    def setUp(self):
        self.cfg = Config(str(BACKEND / "alembic.ini"))
        self.cfg.set_main_option("script_location", str(BACKEND / "migrations"))
        self.script = ScriptDirectory.from_config(self.cfg)

    def test_has_exactly_one_head(self):
        """**只能有一个"最新版本"**。

        为什么在意：如果两个人在各自的分支上各写了一个迁移，
        合并后就会出现两个"最新版本"（叫多头）。这时 alembic 不知道按什么顺序跑，
        升级会直接报错 —— 而且往往是在部署当天才发现，非常尴尬。
        """
        heads = self.script.get_heads()
        self.assertEqual(len(heads), 1, f"迁移出现多头（{len(heads)} 个）：{heads}")

    def test_history_is_linear(self):
        """版本链要一条直线（每个版本只有一个父版本）。

        出现"分叉"意味着有人基于旧版本写了新迁移，
        另一条分支上的改动会被跳过 —— 表现是"升级完发现少了一列"。
        """
        for rev in self.script.walk_revisions():
            down = rev.down_revision
            self.assertNotIsInstance(down, tuple,
                                     f"版本 {rev.revision} 有多个父版本，历史出现分叉")

    def test_every_revision_has_downgrade_or_is_declared_irreversible(self):
        """每个迁移都要想清楚"怎么回滚"。

        不写 downgrade 的后果：改错了想退回去，只能手工改库 ——
        那正是引入迁移工具之前的老问题。
        （真无法回滚的迁移可以显式写 pass，但要留下说明。）
        """
        for rev in self.script.walk_revisions():
            body = pathlib.Path(rev.path).read_text(encoding="utf-8")
            self.assertIn("def downgrade()", body, f"{rev.revision} 缺少 downgrade")


class MigrationMatchesModelsTestCase(unittest.TestCase):
    """**核心体检**：迁移脚本产出的表结构 == 代码里的模型定义。"""

    def test_migrations_match_models_exactly(self):
        """跑完迁移后，表结构必须和模型定义完全一致（差异要为空）。

        这条测试的价值：
          · 有人改了模型忘了写迁移 → 这里报红（否则要等到客户环境才炸）；
          · 迁移里写了模型里不存在的东西 → 也报红（多半是手写迁移时写错了）。
        """
        with tempfile.TemporaryDirectory(prefix="kbqa_mig_") as tmp:
            db_path = pathlib.Path(tmp) / "verify.db"
            cfg = _alembic_config(db_path)
            command.upgrade(cfg, "head")

            engine = create_engine(f"sqlite:///{db_path}")
            with engine.connect() as conn:
                ctx = MigrationContext.configure(
                    conn, opts={"compare_type": True, "render_as_batch": True}
                )
                diffs = compare_metadata(ctx, Base.metadata)
            engine.dispose()

        self.assertEqual(
            diffs, [],
            "迁移脚本与模型定义不一致 —— 说明有人改了模型但没生成迁移，"
            "或者手写的迁移写错了。生产环境会出现"
            "「本地好好的、客户那边一升级就报错」。差异明细：\n"
            + "\n".join(f"  · {d}" for d in diffs[:10]),
        )

    def test_upgrade_creates_all_tables(self):
        """升级要真的把全部业务表建出来（而不是"跑成功了但少建几张"）。"""
        with tempfile.TemporaryDirectory(prefix="kbqa_mig_") as tmp:
            db_path = pathlib.Path(tmp) / "verify.db"
            cfg = _alembic_config(db_path)
            command.upgrade(cfg, "head")

            engine = create_engine(f"sqlite:///{db_path}")
            tables = set(inspect(engine).get_table_names())
            engine.dispose()

        expected = set(Base.metadata.tables.keys())
        missing = expected - tables
        self.assertEqual(missing, set(), f"这些表迁移没建出来：{sorted(missing)}")
        # alembic 自己需要一张小表记录"跑到第几号了"
        self.assertIn("alembic_version", tables)

    def test_downgrade_removes_all_tables(self):
        """回滚要能真的退回去（这是"改错了能救回来"的底气）。

        如果 downgrade 只是写了个 pass（什么都没做），
        看着"回滚成功"，实际上库结构一点没变 —— 那等于没有回滚能力。
        """
        with tempfile.TemporaryDirectory(prefix="kbqa_mig_") as tmp:
            db_path = pathlib.Path(tmp) / "verify.db"
            cfg = _alembic_config(db_path)
            command.upgrade(cfg, "head")
            command.downgrade(cfg, "base")

            engine = create_engine(f"sqlite:///{db_path}")
            tables = set(inspect(engine).get_table_names())
            engine.dispose()

        leftover = tables & set(Base.metadata.tables.keys())
        self.assertEqual(leftover, set(), f"回滚后这些表还留着：{sorted(leftover)}")

    def test_stamp_head_is_idempotent_on_existing_db(self):
        """"认领已有库"这一步要能重复执行（运维手滑跑两次不该出事）。

        为什么需要 stamp：现在正在运行的正式库是**没有** alembic_version 表的。
        直接 upgrade 会以为库是空的、试图重新建表，撞上"表已存在"报错。
        正确做法是先 stamp head（告诉它"结构已经是最新的"）。
        """
        with tempfile.TemporaryDirectory(prefix="kbqa_mig_") as tmp:
            db_path = pathlib.Path(tmp) / "verify.db"
            cfg = _alembic_config(db_path)
            command.upgrade(cfg, "head")
            for _ in range(3):
                command.stamp(cfg, "head")
            # 跑完还是能正常识别为最新状态
            command.upgrade(cfg, "head")

            engine = create_engine(f"sqlite:///{db_path}")
            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn, opts={"compare_type": True})
                self.assertEqual(compare_metadata(ctx, Base.metadata), [],
                                 "重复 stamp 之后结构对比出现差异")
            engine.dispose()

    def test_env_py_imports_all_models(self):
        """env.py 必须导入全部模型。

        这条是防"删表事故"：alembic 自动生成迁移时，靠"模型定义 vs 数据库现状"推导差异。
        如果某个模型没被导入，alembic 会以为它不存在 ——
        于是生成一条**删掉那张表**的迁移。跑下去数据就没了。
        """
        src = (BACKEND / "migrations" / "env.py").read_text(encoding="utf-8")
        self.assertIn("import app.models", src,
                      "env.py 没有导入 app.models，自动生成迁移可能生成删表语句")


if __name__ == "__main__":
    unittest.main()
