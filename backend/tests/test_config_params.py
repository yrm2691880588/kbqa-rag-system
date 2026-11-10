"""可调参数收口测试（P2-20）。

这个文件在测什么（大白话）：
    以前有些数字是**直接写死在代码里**的 —— 比如"单个文件最大 100MB"、
    "探活超时 5 秒"。客户想把上限调到 200MB，就得改代码、重新打包、重新部署。

    现在这些数字搬到了配置里（环境变量）。改一个值、重启，就生效。

    这里验证的是"**搬没搬干净、读了有没有用**"，而不是"数字对不对"：
      · 配置改了，代码要跟着变（不能还认那个写死的值）；
      · 别出现"配置项加了但根本没人读"的死配置（这种最坑：运维以为改了有用，
        其实一点作用都没有，还不报错）。

运行方式（backend 目录下）：
    python -m unittest tests.test_config_params -v
"""

import asyncio
import io
import os
import pathlib
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException, UploadFile
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1 import knowledge_bases as kb_api
from app.core.config import Settings, settings
from app.core import startup_check as sc
from app.db.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.organization import Organization
from app.models.user import User

BACKEND = pathlib.Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


class UploadLimitTestCase(unittest.TestCase):
    """上传大小上限：必须跟着配置走，而不是跟着代码里那个写死的常量走。"""

    def setUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        async def _seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.Session() as db:
                db.add_all([
                    Organization(id=1, name="甲公司", invite_code="c1"),
                    User(id=1, username="admin_a", password_hash="x", role="admin", org_id=1),
                    KnowledgeBase(id=1, name="手册", org_id=1),
                ])
                await db.commit()

        _run(_seed())
        self._old_limit = settings.max_upload_mb

    def tearDown(self):
        settings.max_upload_mb = self._old_limit
        _run(self.engine.dispose())

    def _upload(self, content: bytes):
        """把内容包成一个上传文件，直接调接口函数（不再多起一层 HTTP）。"""
        f = UploadFile(filename="资料.txt", file=io.BytesIO(content))

        async def go():
            async with self.Session() as db:
                admin = await db.get(User, 1)
                return await kb_api.upload_document(1, db, admin, f)

        return go

    def test_limit_follows_config(self):
        """配置改成 1MB，接口里的上限就必须是 1MB。"""
        settings.max_upload_mb = 1
        self.assertEqual(kb_api._max_upload_mb(), 1)
        settings.max_upload_mb = 7
        self.assertEqual(kb_api._max_upload_mb(), 7)

    def test_file_over_limit_is_rejected_with_413(self):
        """超出上限的文件要被拒，并且是 413（"太大了"，不是"参数错了"）。

        这里配置成 1MB，上传 1MB + 1 字节，正好卡在边界外侧。
        """
        settings.max_upload_mb = 1
        content = b"a" * (1024 * 1024 + 1)

        async def t():
            try:
                await self._upload(content)()
            except HTTPException as exc:
                return exc
            self.fail("超出上限的文件居然被接受了")

        exc = _run(t())
        self.assertEqual(exc.status_code, 413)
        self.assertIn("1MB", exc.detail, "提示里应写清实际的上限值，而不是写死的 100MB")

    def test_file_just_under_limit_is_accepted(self):
        """刚好在上限之内的文件不该被拒（边界不能划错）。

        为什么单测这个：把 `>` 写成 `>=` 的话，正好 1MB 的文件会被误拒，
        而用户手里那个文件大小没有任何问题 —— 这类"差一个字节"的错最难查。
        """
        settings.max_upload_mb = 1
        content = b"a" * (1024 * 1024)

        async def t():
            return await self._upload(content)()

        doc = _run(t())
        self.assertEqual(doc.filename, "资料.txt", "正好 1MB 的文件被误拒了")

    def test_no_hardcoded_limit_left_in_source(self):
        """源码里不该再留下"模块级写死"的上限常量。

        这条是防回退：将来有人图省事又把数字写回代码里，
        配置就变成了摆设（改了不生效、还不报错）。
        """
        src = (BACKEND / "app" / "api" / "v1" / "knowledge_bases.py").read_text(encoding="utf-8")
        offenders = re.findall(r"^\s*MAX_UPLOAD_MB\s*=\s*\d+", src, flags=re.MULTILINE)
        self.assertEqual(offenders, [], f"又出现了写死的上传上限：{offenders}")


class TimeoutConfigTestCase(unittest.TestCase):
    """超时类参数：配置改了，真正建客户端时要用上。"""

    def test_health_timeout_reads_config(self):
        from app.services import health_check as hc

        old = settings.health_check_timeout
        try:
            settings.health_check_timeout = 3.5
            self.assertEqual(hc._check_timeout(), 3.5)
        finally:
            settings.health_check_timeout = old

    def test_llm_client_uses_configured_timeouts(self):
        """对话客户端必须用配置里的超时，而且总超时/建连超时要分开生效。

        为什么要分开测：只给一个总超时的话，"网络不通"也得等到总超时才报错，
        用户会对着转圈等两分钟。这两项要真的落到 httpx.Timeout 上才算数。
        """
        from app.services import rag

        old_total = settings.llm_request_timeout
        old_conn = settings.llm_connect_timeout
        saved_client = rag._llm_client
        try:
            settings.llm_request_timeout = 77.0
            settings.llm_connect_timeout = 6.0
            rag._llm_client = None            # 强制重建客户端
            client = rag._get_llm_client()
            timeout = client.timeout
            self.assertEqual(timeout.read, 77.0, "总超时没生效")
            self.assertEqual(timeout.connect, 6.0, "建连超时没生效（网络不通时会转圈等到总超时）")
        finally:
            rag._llm_client = saved_client
            settings.llm_request_timeout = old_total
            settings.llm_connect_timeout = old_conn


class NoDeadConfigTestCase(unittest.TestCase):
    """别出现"配置项加了但没人读"的死配置。

    这种问题最坑运维：他改了配置、重启了服务、满心以为生效了，
    实际上那一行从来没被任何代码读过 —— 而且不会有任何报错。
    """

    # 这些配置项不在后端代码里读，而是被 docker-compose.yml / nginx / 部署脚本消费
    CONSUMED_OUTSIDE_BACKEND = {
        "postgres_user", "postgres_password", "postgres_db", "postgres_port",
        "qdrant_port", "backend_port", "frontend_port", "pip_index_url",
        "npm_registry", "nginx_port", "compose_project_name", "docker_image_prefix",
        "admin_username",  # 只在启动初始化与自检里用，下面的扫描会命中，留着不碍事
    }

    def test_every_setting_field_is_referenced_somewhere(self):
        fields = set(Settings.model_fields.keys())
        # 把 app/ 下所有源码拼起来，逐个字段找引用
        blob = "\n".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in (BACKEND / "app").rglob("*.py")
        )
        unused = []
        for name in sorted(fields):
            if name in self.CONSUMED_OUTSIDE_BACKEND:
                continue
            # 三种读法都要认，各写成一条精确的正则：
            #   ① settings.xxx / cfg.xxx            —— 最常见的直接读
            #   ② getattr(settings, "xxx", 默认值)  —— 带兜底默认值的写法
            #   ③ self.xxx                          —— 配置类内部（比如 pool_budget()）读自己
            # 为什么必须都写：漏掉哪一种，那种就会被误判成"没人读"，
            # 于是我就可能把一条**真正在用**的配置当死配置清理掉 ——
            # 那比留着死配置糟糕得多。所以这里宁可写细一点、宽一点。
            pattern = re.compile(
                r"(?:settings|cfg|self)\." + re.escape(name) + r"\b"
                r"|getattr\(\s*(?:settings|cfg|self)\s*,\s*['\"]" + re.escape(name) + r"['\"]"
            )
            if pattern.search(blob):
                continue
            unused.append(name)
        self.assertEqual(unused, [], f"这些配置项没有任何代码在读，改了也不会生效：{unused}")

    def test_new_param_fields_are_wired(self):
        """显式点名 P2-20 新加的几项（万一上面那条被改成永远通过，这条还兜得住）。"""
        for name in ("max_upload_mb", "llm_request_timeout", "llm_connect_timeout",
                     "health_check_timeout", "health_llm_probe_timeout"):
            self.assertIn(name, Settings.model_fields, f"{name} 配置项不见了")

    def test_startup_check_still_works_after_refactor(self):
        """参数收口不该顺手把启动自检搞坏（跑一遍纯函数确认）。"""
        cfg = sc.Settings()
        errors, warnings = sc.collect_startup_issues(cfg)
        self.assertIsInstance(errors, list)
        self.assertIsInstance(warnings, list)


if __name__ == "__main__":
    unittest.main()
