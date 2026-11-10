"""数据库连接池测试（P2-17）。

这个文件在测什么（大白话）：
    连接池 = "预先备好几条到数据库的电话线，谁用谁占，用完立刻还"。
    这里验证两件最容易出事、而且平时看不出来的事情：

    ① **"电话线"总数够不够**：一个进程最多拿几条 × 跑几个进程，
       不能超过数据库允许的总数。超了就会"平时好好的、一忙就连不上数据库"。
       所以要把这道算术题算清楚（不是猜，是算）。

    ② **借了必须还**：接口用完连接一定要还回池里。
       漏还的后果是池子被一点点耗干，最后全站卡死；
       而漏还通常只发生在**异常路径**上（正常路径反而是好的），
       所以专门测"出错之后连接有没有还回来"。

运行方式（backend 目录下）：
    python -m unittest tests.test_db_pool -v
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.core import startup_check as sc


class PoolBudgetTestCase(unittest.TestCase):
    """连接总量这道算术题。"""

    def _cfg(self, **kw):
        cfg = sc.Settings()
        for k, v in kw.items():
            setattr(cfg, k, v)
        return cfg

    def test_budget_multiplies_by_workers(self):
        """总量 = 进程数 ×（常备 + 可超借）。"""

        cfg = self._cfg(db_pool_size=5, db_max_overflow=10, web_concurrency=4,
                        db_max_connections=100)
        b = cfg.pool_budget()
        self.assertEqual(b["per_worker"], 15)
        self.assertEqual(b["workers"], 4)
        self.assertEqual(b["total"], 60)
        self.assertFalse(b["over_budget"], "4×15=60 < 100，不该报超")

    def test_over_budget_is_detected(self):
        """超出数据库上限要能被判出来 —— 这正是"平时正常、一忙就崩"的根因。"""
        cfg = self._cfg(db_pool_size=10, db_max_overflow=10, web_concurrency=8,
                        db_max_connections=100)
        b = cfg.pool_budget()
        self.assertEqual(b["total"], 160)
        self.assertTrue(b["over_budget"], "8×20=160 > 100，必须判为超")

    def test_workers_at_least_one(self):
        """进程数写 0 或负数时要当成 1，不能算出"0 条连接"这种荒唐结果。"""
        for bad in (0, -3):
            b = self._cfg(web_concurrency=bad).pool_budget()
            self.assertEqual(b["workers"], 1)
            self.assertGreater(b["total"], 0)

    def test_budget_is_pure_function_of_config(self):
        """这个算术只依赖配置，不碰任何数据库连接对象。

        为什么要单独钉住：启动自检要在"数据库地址还没填"的时候就能跑出结论。
        如果算这个数需要先连数据库，那它会先崩掉，把真正要报的错（地址没填）盖住 ——
        用户看到的是一堆看不懂的内部报错。
        """
        cfg = self._cfg(database_url="")   # 故意留空
        b = cfg.pool_budget()              # 不该抛任何异常
        self.assertIn("total", b)


class StartupCheckPoolTestCase(unittest.TestCase):
    """启动自检里要给出结论（够用就报数字，不够就明确警告）。"""

    def _issues(self, **kw):
        cfg = sc.Settings()
        cfg.app_env = "prod"
        cfg.secret_key = "x" * 40
        cfg.llm_api_key = "k"
        cfg.embedding_api_key = "k"
        cfg.database_url = "postgresql+asyncpg://u:p@postgres:5432/kbqa"
        cfg.sms_provider = "aliyun"
        cfg.sms_aliyun_access_key_id = "i"
        cfg.sms_aliyun_access_key_secret = "s"
        cfg.sms_aliyun_sign_name = "sign"
        cfg.sms_aliyun_template_code = "tpl"
        for k, v in kw.items():
            setattr(cfg, k, v)
        return sc.collect_startup_issues(cfg)

    def test_over_budget_warns_with_numbers(self):
        errors, warnings = self._issues(db_pool_size=10, db_max_overflow=10,
                                        web_concurrency=8, db_max_connections=100)
        hit = [w for w in warnings if "连接可能不够用" in w]
        self.assertTrue(hit, f"连接超预算却没有提醒：{warnings}")
        # 提示里必须把算式摊开，运维才知道该调哪个数
        self.assertIn("160", hit[0])
        self.assertIn("100", hit[0])
        self.assertIn("DB_POOL_SIZE", hit[0])

    def test_sufficient_budget_does_not_warn(self):
        errors, warnings = self._issues(db_pool_size=5, db_max_overflow=10,
                                        web_concurrency=4, db_max_connections=100)
        self.assertFalse([w for w in warnings if "连接可能不够用" in w], str(warnings))

    def test_dev_does_not_warn(self):
        """开发环境不打扰（本地单进程，怎么配都不会超）。"""
        cfg = sc.Settings()
        cfg.app_env = "dev"
        cfg.db_pool_size = 10
        cfg.db_max_overflow = 10
        cfg.web_concurrency = 8
        cfg.db_max_connections = 100
        _, warnings = sc.collect_startup_issues(cfg)
        self.assertFalse([w for w in warnings if "连接可能不够用" in w])


class PoolStatusTestCase(unittest.TestCase):
    """连接池状态要能看得到（这是"有没有人借了不还"最直接的观测点）。"""

    def test_pool_status_shape_on_sqlite(self):
        """SQLite 没有标准连接池，取值要返回空字典而不是抛异常。

        为什么在意：健康检查会调它。如果拿不到状态就抛异常，
        整条健康检查会变成"不健康"—— 明明服务好好的，监控却在报警。
        """
        from app.db import session as db_session

        st = db_session.pool_status()
        self.assertIsInstance(st, dict)
        if st:   # 有池实现时，四个字段都要在
            for key in ("size", "checked_out", "overflow", "available"):
                self.assertIn(key, st)

    def test_get_db_returns_connection_after_use(self):
        """借出去的连接必须还回来（正常的用完路径）。"""
        from app.db import session as db_session

        async def t():
            before = db_session.pool_status()
            async for _ in db_session.get_db():
                break        # 只拿会话体，不执行任何语句
            after = db_session.pool_status()
            return before, after

        before, after = asyncio.run(t())
        if before and after:
            self.assertLessEqual(after["checked_out"], before["checked_out"],
                                 "用完连接没有归还，池子会被慢慢耗干")

    def test_get_db_returns_connection_on_exception(self):
        """**关键**：接口内部抛异常时，连接也必须还回来。

        这是最容易漏的一条路径 —— 正常写完就 return 的代码大家都记得还连接，
        但异常分支一不留神就漏了。漏几次之后池子就干了，
        表现是"个别请求报错之后，越来越多请求开始报错"。
        """
        from app.db import session as db_session

        async def t():
            before = db_session.pool_status()
            gen = db_session.get_db()
            await gen.__anext__()          # 取到会话（相当于连接被借出）
            with self.assertRaises(RuntimeError):
                await gen.athrow(RuntimeError("模拟接口内部出错"))
            after = db_session.pool_status()
            return before, after

        before, after = asyncio.run(t())
        if before and after:
            self.assertLessEqual(
                after["checked_out"], before["checked_out"],
                "异常路径把连接漏掉了（这种漏法在高峰期会让全站卡死）",
            )


class ConfiguredPoolSettingsTestCase(unittest.TestCase):
    """池子参数要真的被读进引擎，而不是加了个配置没人用。

    ⚠️ 只对 PostgreSQL 有意义：
        SQLite（测试/CI 环境用的内存库）走的是 StaticPool，根本没有
        pool_size / max_overflow 这些概念，`pool.size()` 方法也不存在。
        在 SQLite 下跳过，而不是让用例报一个与配置无关的 AttributeError。
    """

    @unittest.skipIf(settings.is_sqlite, "SQLite 使用 StaticPool，没有连接池参数可验")
    def test_engine_uses_configured_pool_params(self):
        import importlib

        old = (settings.db_pool_size, settings.db_max_overflow,
               settings.db_pool_timeout, settings.db_pool_recycle_seconds)
        try:
            settings.db_pool_size = 7
            settings.db_max_overflow = 3
            settings.db_pool_timeout = 11.0
            settings.db_pool_recycle_seconds = 900

            from app.db import session as db_session

            eng = importlib.reload(db_session).engine   # 重新按配置建引擎
            pool = eng.pool
            self.assertEqual(pool.size(), 7, "pool_size 配置没生效")
            self.assertEqual(pool._max_overflow, 3, "max_overflow 配置没生效")
            self.assertEqual(pool._timeout, 11.0, "pool_timeout 配置没生效")
            self.assertEqual(pool._recycle, 900, "pool_recycle 配置没生效")
            asyncio.run(eng.dispose())
        finally:
            (settings.db_pool_size, settings.db_max_overflow,
             settings.db_pool_timeout, settings.db_pool_recycle_seconds) = old
            import importlib as _il
            from app.db import session as _s
            _il.reload(_s)   # 还原成配置里的正常值


if __name__ == "__main__":
    unittest.main()
