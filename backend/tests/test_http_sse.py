"""HTTP 层集成测试：SSE 流式问答。

为什么单独测 SSE（大白话）：
    流式问答是"边生成边往外吐"，它的错误形态和普通接口完全不同：
      · 普通接口出错 → 返回一个错误 JSON，一目了然；
      · 流式接口出错 → HTTP 状态码可能还是 200，错误藏在事件流里，
        或者流直接断掉，前端只会看到"转圈不动"。
    所以必须专门验证：事件格式对不对、越权会不会被挡住、
    上游模型挂了会不会把内部报错原样吐给用户。

本文件用 LLM_MOCK=true 走本地模拟流，不调真实 DeepSeek，不花钱。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.models.session import ChatSession  # noqa: E402
from tests.http_base import HttpTestCase  # noqa: E402


def parse_sse(text: str) -> list[dict]:
    """把 SSE 文本解析成事件列表。

    本项目的事件格式是两行一组（见 chat.py 的 _sse）：
        event: answer
        data: {"text": "..."}
        （空行）
    所以既要读 event 名，也要读 data 内容。
    """
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload:
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        data = None
        if name or data:
            ev = {"type": name}
            if isinstance(data, dict):
                ev.update(data)
            events.append(ev)
    return events


class SSEChatTestCase(HttpTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.org = await self.make_org("流式公司")
        self.user = await self.make_user("sse1", role="admin", org_id=self.org.id)
        async with self.Session() as s:
            kb = KnowledgeBase(name="测试库", org_id=self.org.id, is_active=True)
            s.add(kb)
            await s.commit()
            await s.refresh(kb)
            self.kb_id = kb.id
            sess = ChatSession(user_id=self.user.id, title="测试会话", kb_scope=[kb.id])
            s.add(sess)
            await s.commit()
            await s.refresh(sess)
            self.session_id = sess.id

    async def test_chat_requires_token(self):
        r = await self.client.post(
            "/api/v1/chat", json={"session_id": self.session_id, "question": "你好"}
        )
        self.assertEqual(r.status_code, 401, "没登录也能发起问答（会消耗额度）")

    async def test_stream_returns_valid_sse_events(self):
        """正常问答：必须是 text/event-stream，且事件能被解析、以 done 收尾。"""
        r = await self.client.post(
            "/api/v1/chat",
            headers=self.auth_headers(self.user),
            json={"session_id": self.session_id, "question": "你好"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn(
            "text/event-stream", r.headers.get("content-type", ""),
            "返回的不是事件流，前端无法做打字机效果",
        )
        events = parse_sse(r.text)
        self.assertTrue(events, "事件流里解析不出任何事件")
        types = [e.get("type") for e in events]
        self.assertIn("done", types, f"流没有以 done 事件收尾（前端会一直转圈）：{types}")

    async def test_stream_has_no_internal_error_leak(self):
        """任何情况下都不能把内部异常栈/密钥吐给用户。"""
        r = await self.client.post(
            "/api/v1/chat",
            headers=self.auth_headers(self.user),
            json={"session_id": self.session_id, "question": "测试"},
        )
        body = r.text.lower()
        for leaked in ("traceback", "secret", "api_key", "password", "sk-"):
            self.assertNotIn(leaked, body, f"响应流里出现了敏感字样：{leaked}")

    async def test_cannot_chat_in_other_users_session(self):
        """用别人的 session_id 发起问答必须被拒。

        为什么重要：会话里存着对话历史，能往别人的会话里写东西，
        等于能篡改别人的对话记录，也能借别人的额度。
        """
        other = await self.make_user("other1", role="admin", org_id=self.org.id)
        async with self.Session() as s:
            s2 = ChatSession(user_id=other.id, title="别人的会话", kb_scope=[self.kb_id])
            s.add(s2)
            await s.commit()
            await s.refresh(s2)
            other_sid = s2.id

        r = await self.client.post(
            "/api/v1/chat",
            headers=self.auth_headers(self.user),
            json={"session_id": other_sid, "question": "偷看别人的对话"},
        )
        self.assertIn(
            r.status_code, (403, 404),
            f"能用别人的 session_id 发起问答（status={r.status_code}）",
        )

    async def test_empty_question_rejected(self):
        r = await self.client.post(
            "/api/v1/chat",
            headers=self.auth_headers(self.user),
            json={"session_id": self.session_id, "question": ""},
        )
        self.assertEqual(r.status_code, 422, "空问题没有被参数校验挡住")

    async def test_question_too_long_rejected(self):
        r = await self.client.post(
            "/api/v1/chat",
            headers=self.auth_headers(self.user),
            json={"session_id": self.session_id, "question": "问" * 2001},
        )
        self.assertEqual(r.status_code, 422, "超长问题没有被挡住（可能撑爆上下文）")


if __name__ == "__main__":
    unittest.main()
