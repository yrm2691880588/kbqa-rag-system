"""HTTP 层集成测试：鉴权依赖、越权防护、路由守卫。

为什么单独建这个文件（大白话）：
    之前的 435 个用例全是"直接调函数"，把 FastAPI 的鉴权层整个跳过了。
    后果是：如果有人把 @router 上的权限依赖删了，或者把 get_current_user
    的令牌校验写松了，那 435 个测试**依然全绿**——因为它们压根没走那条路。
    真实事故（越权、漏鉴权）恰恰就发生在这层。

本文件用真实 HTTP 请求验证三件事：
    ① 没令牌 / 令牌坏了 / 令牌过期 → 必须 401，不能放进来；
    ② 普通成员调管理员接口 → 必须 403，不能因为"前端没显示按钮"就算防住了；
    ③ 跨组织访问 → 必须 404/403，不能通过改 id 拿到别家数据。

这三条是"服务端必须自己挡住"的底线，前端隐藏按钮不算防护。
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from tests.http_base import HttpTestCase  # noqa: E402


class NoTokenTestCase(HttpTestCase):
    """没有任何令牌时必须 401 —— 这是最基本的一道门。"""

    async def test_users_me_requires_token(self):
        r = await self.client.get("/api/v1/users/me")
        self.assertEqual(r.status_code, 401, "没带令牌也能读到用户信息")

    async def test_kb_list_requires_token(self):
        r = await self.client.get("/api/v1/knowledge-bases")
        self.assertEqual(r.status_code, 401, "没带令牌也能列知识库")

    async def test_sessions_requires_token(self):
        r = await self.client.get("/api/v1/sessions")
        self.assertEqual(r.status_code, 401, "没带令牌也能列会话")

    async def test_query_logs_requires_token(self):
        r = await self.client.get("/api/v1/query-logs")
        self.assertEqual(r.status_code, 401, "没带令牌也能看问答日志（含客户真实提问）")

    async def test_orgs_requires_token(self):
        r = await self.client.get("/api/v1/organizations")
        self.assertEqual(r.status_code, 401, "没带令牌也能列组织")


class BadTokenTestCase(HttpTestCase):
    """令牌存在但不可信时，一律拒绝。"""

    async def test_malformed_token_rejected(self):
        r = await self.client.get(
            "/api/v1/users/me", headers={"Authorization": "Bearer not-a-jwt"}
        )
        self.assertEqual(r.status_code, 401)

    async def test_wrong_signature_rejected(self):
        # 用错误的密钥签一个结构合法但签名不对的令牌，模拟伪造
        import jwt

        forged = jwt.encode({"sub": "1", "role": "admin", "type": "access"}, "wrong-secret", algorithm="HS256")
        r = await self.client.get(
            "/api/v1/users/me", headers={"Authorization": f"Bearer {forged}"}
        )
        self.assertEqual(r.status_code, 401, "伪造签名的令牌被接受了")

    async def test_expired_token_rejected(self):
        import datetime as dt

        import jwt

        from app.core.config import settings

        payload = {
            "sub": "1",
            "role": "admin",
            "type": "access",
            "iat": dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5),
            "exp": dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1),
        }
        expired = jwt.encode(payload, settings.secret_key, algorithm="HS256")
        r = await self.client.get(
            "/api/v1/users/me", headers={"Authorization": f"Bearer {expired}"}
        )
        self.assertEqual(r.status_code, 401, "过期令牌被接受了")

    async def test_refresh_token_cannot_be_used_as_access(self):
        """刷新令牌只能换新令牌，不能当访问令牌直接调接口。

        为什么单独测：两种令牌用同一个密钥签，如果代码只看"签名对不对"
        而不看 type 字段，那么有效期 7 天的刷新令牌就能当访问令牌用，
        等于把短令牌的安全设计整个废掉。
        """
        from app.core.security import create_refresh_token

        u = await self.make_user("bob", role="admin")
        rt = create_refresh_token(user_id=u.id, role="admin")
        r = await self.client.get(
            "/api/v1/users/me", headers={"Authorization": f"Bearer {rt}"}
        )
        self.assertEqual(r.status_code, 401, "刷新令牌被当成访问令牌用了")


class RoleGuardTestCase(HttpTestCase):
    """普通成员调管理员接口必须 403。

    为什么要测：前端隐藏按钮只是"看不见"，不是"做不到"。
    用户完全可以自己构造请求。服务端必须自己挡。
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.org = await self.make_org("甲公司")
        self.member = await self.make_user("member1", role="member", org_id=self.org.id)
        self.admin = await self.make_user("admin1", role="admin", org_id=self.org.id)
        self.superadmin = await self.make_user(
            "root", role="admin", org_id=self.org.id, is_superadmin=True
        )

    async def test_member_cannot_list_users(self):
        r = await self.client.get("/api/v1/users", headers=self.auth_headers(self.member))
        self.assertEqual(r.status_code, 403, "普通成员能列出全部成员")

    async def test_member_cannot_read_query_logs(self):
        r = await self.client.get("/api/v1/query-logs", headers=self.auth_headers(self.member))
        self.assertEqual(r.status_code, 403, "普通成员能看问答日志")

    async def test_member_cannot_create_knowledge_base(self):
        r = await self.client.post(
            "/api/v1/knowledge-bases",
            headers=self.auth_headers(self.member),
            json={"name": "偷建的库"},
        )
        self.assertEqual(r.status_code, 403, "普通成员能新建知识库")

    async def test_admin_can_list_users(self):
        r = await self.client.get("/api/v1/users", headers=self.auth_headers(self.admin))
        self.assertEqual(r.status_code, 200, "组织管理员反而被挡住了")

    async def test_org_admin_cannot_create_organization(self):
        """组织管理员不能新建组织 —— 那是平台运营方的权限。"""
        r = await self.client.post(
            "/api/v1/organizations",
            headers=self.auth_headers(self.admin),
            json={"name": "我自己开的公司"},
        )
        self.assertEqual(r.status_code, 403, "组织管理员能给自己开新组织（计费与隔离会失控）")

    async def test_superadmin_can_list_organizations(self):
        r = await self.client.get(
            "/api/v1/organizations", headers=self.auth_headers(self.superadmin)
        )
        self.assertEqual(r.status_code, 200, "平台超管反而看不了组织列表")

    async def test_health_deep_requires_admin(self):
        """/health/deep 会真实调用外部 AI 产生费用，必须要求管理员。"""
        r = await self.client.get("/api/v1/health/deep")
        self.assertEqual(r.status_code, 401, "未登录也能触发深度探活（可被刷爆配额）")

        r2 = await self.client.get(
            "/api/v1/health/deep", headers=self.auth_headers(self.member)
        )
        self.assertEqual(r2.status_code, 403, "普通成员能触发深度探活")

    async def test_health_live_and_ready_are_public(self):
        """存活/就绪探针必须保持匿名可访问，否则容器编排无法探活。"""
        for path in ("/api/v1/health/live", "/api/v1/health/ready"):
            r = await self.client.get(path)
            self.assertIn(r.status_code, (200, 503), f"{path} 不该要求登录")


class CrossOrgTestCase(HttpTestCase):
    """跨组织访问必须被挡住 —— 这是多租户的命门。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.org_a = await self.make_org("甲公司")
        self.org_b = await self.make_org("乙公司")
        self.user_a = await self.make_user("ua", role="admin", org_id=self.org_a.id)
        self.user_b = await self.make_user("ub", role="admin", org_id=self.org_b.id)

        # 乙公司建一个库
        async with self.Session() as s:
            kb = KnowledgeBase(name="乙公司机密", org_id=self.org_b.id, is_active=True)
            s.add(kb)
            await s.commit()
            await s.refresh(kb)
            self.kb_b_id = kb.id

    async def test_cannot_see_other_org_kb_in_list(self):
        r = await self.client.get(
            "/api/v1/knowledge-bases", headers=self.auth_headers(self.user_a)
        )
        self.assertEqual(r.status_code, 200)
        names = [item["name"] for item in r.json()]
        self.assertNotIn("乙公司机密", names, "甲公司的管理员看到了乙公司的知识库")

    async def test_cannot_read_other_org_kb_documents(self):
        """直接按 id 访问别家知识库下的文档列表 —— 前端不显示，但用户能自己构造请求。

        注意：这里测的是真实存在的路由 /knowledge-bases/{kb_id}/documents，
        不是凭空想象的"详情接口"（项目没有那个端点，写错会得到 405 而不是越权结论）。
        """
        r = await self.client.get(
            f"/api/v1/knowledge-bases/{self.kb_b_id}/documents",
            headers=self.auth_headers(self.user_a),
        )
        self.assertIn(
            r.status_code, (403, 404),
            f"甲公司的管理员用 id 读到了乙公司库里的文档（status={r.status_code}）",
        )

    async def test_cannot_delete_other_org_kb(self):
        r = await self.client.delete(
            f"/api/v1/knowledge-bases/{self.kb_b_id}",
            headers=self.auth_headers(self.user_a),
        )
        self.assertIn(
            r.status_code, (403, 404),
            f"甲公司的管理员删掉了乙公司的库（status={r.status_code}）",
        )

    async def test_orphan_user_sees_no_kb(self):
        """没有归属组织的账号，必须一个库都看不到。

        这是 code review 发现过的真实缺陷：早期实现里
        `org_id == None` 被 SQL 渲染成 `IS NULL`，反而能看到所有
        没有归属的"孤儿库"。这里锁死这个行为。
        """
        orphan = await self.make_user("orphan", role="admin", org_id=None)
        r = await self.client.get(
            "/api/v1/knowledge-bases", headers=self.auth_headers(orphan)
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [], "无组织账号看到了知识库（越权）")


if __name__ == "__main__":
    unittest.main()
