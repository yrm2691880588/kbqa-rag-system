"""HTTP 层集成测试基座：用真实 ASGI 请求走完整链路。

为什么需要这个文件（大白话）：
    之前的 435 个测试全是"直接调函数"——它们跳过了 FastAPI 的依赖注入、
    鉴权装饰器、请求校验、中间件、响应序列化。结果是：
      · 鉴权写错了，测试发现不了（因为它根本没走鉴权那一层）；
      · SSE 流断了，测试发现不了（因为没有真实 HTTP 流）；
      · 路由守卫漏了，测试发现不了（因为没经过路由）。
    真实的安全事故（比如越权、漏鉴权）恰恰都藏在这些"被跳过"的层里。

这里做的事：
    用 httpx 的 ASGITransport 把请求直接喂给 FastAPI 应用对象，
    **不启动真实服务器、不占端口**，但走的是和线上完全一样的
    中间件 → 路由 → 依赖注入 → 业务函数 → 响应 整条链路。

用法：
    class MyTestCase(HttpTestCase):
        async def test_xxx(self):
            r = await self.client.get("/api/v1/users/me")
            ...

    需要"以某个用户身份发请求"时用 self.auth_headers(user) 拿到带令牌的请求头。
"""

import asyncio
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.security import create_access_token, hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.user import User  # noqa: E402


class HttpTestCase(unittest.IsolatedAsyncioTestCase):
    """带真实 HTTP 客户端 + 独立内存数据库的测试基座。

    每个测试方法都拿到一套全新的内存库，互不干扰（没有"上一个用例留下的脏数据"）。
    """

    async def asyncSetUp(self):
        from app.main import app

        self.app = app
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            # 内存库的关键：必须用 StaticPool，否则每个连接是各自独立的一个空库
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        async def _override_get_db():
            async with self.Session() as s:
                yield s

        self.app.dependency_overrides[get_db] = _override_get_db
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.app.dependency_overrides.clear()
        await self.engine.dispose()

    # ---------- 造数据的便捷方法 ----------

    async def make_org(self, name: str = "测试公司") -> Organization:
        async with self.Session() as s:
            org = Organization(name=name, invite_code=f"inv-{name}")
            s.add(org)
            await s.commit()
            await s.refresh(org)
            return org

    async def make_user(
        self,
        username: str = "u1",
        *,
        password: str = "Passw0rd123",
        role: str = "member",
        org_id: int | None = None,
        is_superadmin: bool = False,
        is_active: bool = True,
    ) -> User:
        async with self.Session() as s:
            u = User(
                username=username,
                password_hash=hash_password(password),
                role=role,
                org_id=org_id,
                is_superadmin=is_superadmin,
                is_active=is_active,
            )
            s.add(u)
            await s.commit()
            await s.refresh(u)
            return u

    def auth_headers(self, user: User) -> dict[str, str]:
        """生成这个用户的 Authorization 头（真实 JWT，走真实解码逻辑）。"""
        token = create_access_token(user_id=user.id, role=user.role or "member")
        return {"Authorization": f"Bearer {token}"}
