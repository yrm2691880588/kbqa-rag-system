"""数据库连接配置的报错质量测试。

为什么专门测这个：
    `app/db/session.py` 是在**导入时**就建立连接池的。
    如果数据库连接地址没填或写错，默认会抛出一大段 SQLAlchemy 内部报错，
    用户（尤其是非技术用户）根本看不出"其实是配置没填"。
    所以这里锁住两条底线：
      1. 提示必须说人话、点名 DATABASE_URL；
      2. 报错里**不能带出厂址原文**（含数据库口令，会进日志）。
"""

import unittest
from unittest.mock import patch

from app.db import session as session_module


class TestBuildEngineConfigErrors(unittest.TestCase):
    """配置缺失/写错时，应当给出可读的提示。"""

    def test_empty_url_gives_readable_error(self):
        """空地址：提示里必须点名 DATABASE_URL。"""
        with patch.object(session_module.settings, "database_url", ""):
            with self.assertRaises(RuntimeError) as ctx:
                session_module._build_engine()
        msg = str(ctx.exception)
        self.assertIn("DATABASE_URL", msg)
        self.assertIn("未配置", msg)

    def test_whitespace_url_treated_as_empty(self):
        """只有空格也算没填（避免用户误以为填了）。"""
        with patch.object(session_module.settings, "database_url", "    "):
            with self.assertRaises(RuntimeError) as ctx:
                session_module._build_engine()
        self.assertIn("未配置", str(ctx.exception))

    def test_malformed_url_gives_readable_error(self):
        """格式错误：也要是人话，而不是库内部报错。"""
        with patch.object(session_module.settings, "database_url", "这是一个乱写的地址"):
            with self.assertRaises(RuntimeError) as ctx:
                session_module._build_engine()
        msg = str(ctx.exception)
        self.assertIn("格式不正确", msg)
        self.assertIn("DATABASE_URL", msg)

    def test_error_message_does_not_leak_password(self):
        """报错信息里不能出现连接串原文（含口令），否则口令会进日志。"""
        secret = "SuperSecretPassword123"
        with patch.object(
            session_module.settings,
            "database_url",
            f"这是一个乱写的地址-{secret}",
        ):
            with self.assertRaises(RuntimeError) as ctx:
                session_module._build_engine()
        self.assertNotIn(secret, str(ctx.exception))

    def test_valid_url_builds_engine(self):
        """正常地址必须能建出引擎（反向对照：别把好配置也拦了）。"""
        with patch.object(
            session_module.settings,
            "database_url",
            "sqlite+aiosqlite:///:memory:",
        ):
            engine = session_module._build_engine()
        self.assertIsNotNone(engine)


if __name__ == "__main__":
    unittest.main()
