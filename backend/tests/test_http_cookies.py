"""HttpOnly Cookie 认证测试。

这个文件在测什么（大白话）：
    原来令牌存在 localStorage，任何 JS 都能读走 —— 一处 XSS 就等于账号被盗。
    改成 HttpOnly Cookie 后，浏览器自动携带但 JS 读不到，XSS 拿不到令牌原文。
    但"改成 Cookie"会带来两个新风险，必须测死：
      ① Cookie 属性写错（漏了 HttpOnly/Secure/SameSite）→ 安全性等于没改；
      ② 退出登录没清 Cookie → 用户以为退了，实际还能继续用。

本文件验证：
    · 登录后响应里种下 Cookie，且带 httponly / samesite
    · 用 Cookie（不带 Authorization 头）能正常访问受保护接口
    · 退出登录后 Cookie 被清除，旧会话失效
    · 伪造/篡改的 Cookie 被拒绝
    · Authorization 头仍然可用（兼容脚本与第三方调用）
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.auth_cookies import ACCESS_COOKIE, REFRESH_COOKIE  # noqa: E402
from tests.http_base import HttpTestCase  # noqa: E402


class LoginSetsCookiesTestCase(HttpTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.org = await self.make_org("Cookie公司")
        self.user = await self.make_user(
            "cookie1", password="Passw0rd123", role="admin", org_id=self.org.id
        )

    async def test_login_sets_httponly_cookies(self):
        """登录成功必须种下双 Cookie，且带 HttpOnly。"""
        r = await self.client.post(
            "/api/v1/auth/login",
            json={"username": "cookie1", "password": "Passw0rd123"},
        )
        self.assertEqual(r.status_code, 200, r.text[:200])

        # httpx 会把 Set-Cookie 收集到 cookies，但安全属性要从原始头里看
        set_cookie_headers = r.headers.get_list("set-cookie")
        joined = " ; ".join(set_cookie_headers).lower()
        self.assertIn("httponly", joined, "Cookie 没有 HttpOnly 标记 —— 等于没防 XSS")
        self.assertIn("samesite", joined, "Cookie 没有 SameSite 标记 —— 有 CSRF 风险")
        self.assertIn("path=/", joined, "Cookie 的 path 不是全站，部分接口会拿不到")

        self.assertIn(ACCESS_COOKIE, self.client.cookies, "访问令牌 Cookie 没种上")
        self.assertIn(REFRESH_COOKIE, self.client.cookies, "刷新令牌 Cookie 没种上")

    async def test_cookie_auth_works_without_bearer_header(self):
        """只靠 Cookie（完全不带 Authorization 头）必须能访问受保护接口。"""
        r = await self.client.post(
            "/api/v1/auth/login",
            json={"username": "cookie1", "password": "Passw0rd123"},
        )
        self.assertEqual(r.status_code, 200)

        r2 = await self.client.get("/api/v1/users/me")  # 注意：没加任何 header
        self.assertEqual(r2.status_code, 200, "带 Cookie 却没通过鉴权")
        self.assertEqual(r2.json()["username"], "cookie1")

    async def test_bearer_header_still_works(self):
        """兼容性：Authorization 头仍然可用（脚本/压测/第三方调用）。"""
        headers = self.auth_headers(self.user)
        r = await self.client.get("/api/v1/users/me", headers=headers)
        self.assertEqual(r.status_code, 200, "Bearer 头被砍掉了，脚本类调用会全挂")

    async def test_logout_clears_cookies_and_revokes(self):
        """退出登录必须清 Cookie，之后 Cookie 不再能通过鉴权。"""
        await self.client.post(
            "/api/v1/auth/login",
            json={"username": "cookie1", "password": "Passw0rd123"},
        )
        r_ok = await self.client.get("/api/v1/users/me")
        self.assertEqual(r_ok.status_code, 200)

        r_out = await self.client.post("/api/v1/auth/logout", json={})
        self.assertIn(r_out.status_code, (204, 200), f"退出失败：{r_out.text[:200]}")

        # 清完之后，Cookie 应该已被删除（httpx 会同步 Set-Cookie 的删除指令）
        r_after = await self.client.get("/api/v1/users/me")
        self.assertEqual(r_after.status_code, 401, "退出登录后 Cookie 仍然有效")

    async def test_wrong_password_does_not_set_cookie(self):
        """登录失败绝不能种 Cookie。"""
        r = await self.client.post(
            "/api/v1/auth/login",
            json={"username": "cookie1", "password": "wrong-password"},
        )
        self.assertEqual(r.status_code, 401)
        self.assertNotIn(ACCESS_COOKIE, self.client.cookies, "登录失败却种下了访问令牌")

    async def test_refresh_via_cookie_only(self):
        """只用 Cookie 刷新令牌（请求体为空）必须成功。

        回归测试：这里曾经因为"SQLite 读回的 expires_at 是不带时区的 naive
        datetime，与 aware 的 now() 比较"而直接 500。
        服务端必须能处理这种情况，否则本地开发/CI 环境刷新令牌全挂。
        """
        r = await self.client.post(
            "/api/v1/auth/login",
            json={"username": "cookie1", "password": "Passw0rd123"},
        )
        self.assertEqual(r.status_code, 200)

        r2 = await self.client.post("/api/v1/auth/refresh", json={})
        self.assertEqual(r2.status_code, 200, f"Cookie 刷新失败：{r2.text[:300]}")
        self.assertIn("access_token", r2.json())

        # 刷新后仍能正常访问（新 Cookie 已种下）
        r3 = await self.client.get("/api/v1/users/me")
        self.assertEqual(r3.status_code, 200, "刷新后反而不能访问了")


class TamperedCookieTestCase(HttpTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.org = await self.make_org("篡改公司")
        self.user = await self.make_user("tamper1", role="admin", org_id=self.org.id)

    async def test_forged_cookie_rejected(self):
        """手写的假 Cookie 必须被拒（签名校验）。"""
        self.client.cookies.set(ACCESS_COOKIE, "totally-fake-token")
        r = await self.client.get("/api/v1/users/me")
        self.assertEqual(r.status_code, 401, "伪造的 Cookie 被接受了")

    async def test_refresh_token_cookie_cannot_access_api(self):
        """刷新令牌塞进访问 Cookie 也不行（type 校验）。"""
        from app.core.security import create_refresh_token

        self.client.cookies.set(
            ACCESS_COOKIE, create_refresh_token(user_id=self.user.id, role="admin")
        )
        r = await self.client.get("/api/v1/users/me")
        self.assertEqual(r.status_code, 401, "刷新令牌被当成访问令牌用了")


if __name__ == "__main__":
    unittest.main()
