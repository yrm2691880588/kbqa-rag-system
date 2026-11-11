"""日志与请求追踪测试。

这个文件在测什么（大白话）：
    日志是排查问题的唯一线索，所以它有两个"必须守住"的点：

    ① **密钥绝不能进日志**。日志文件权限往往很松（运维群里随手发一段出来
       排查很常见），一旦密钥进了日志就等于泄露了。而且这种泄露不会报错、
       不会有人发现 —— 所以必须靠自动化断言守住，不能靠"我们注意一点"。

    ② **同一个请求的日志要能串起来**。一个用户说"我点了没反应"，
       你得能一条命令捞出这次请求的所有日志。靠"按时间翻文件"是查不动的。

运行方式（backend 目录下）：
    python -m unittest tests.test_logging_setup -v
"""

import asyncio
import json
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import logging_setup as ls


class RedactTestCase(unittest.TestCase):
    """脱敏：这一组是"泄露防线"，宁可多打码也不能漏。"""

    def test_api_keys_are_masked(self):
        self.assertNotIn("abcdefghijklmn", ls.redact("调用失败 key=sk-abcdefghijklmn"))
        self.assertNotIn("abcdefghijklmn", ls.redact("Key 是 kbqa_abcdefghijklmn"))
        # 但前缀要留着，方便判断"是哪一类凭证"
        self.assertIn("sk-", ls.redact("key=sk-abcdefghijklmn"))
        self.assertIn("kbqa_", ls.redact("kbqa_abcdefghijklmn"))

    def test_bearer_token_is_masked(self):
        text = ls.redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", text)
        self.assertIn("Bearer", text)

    def test_phone_number_is_masked(self):
        """手机号是个人信息，日志里不该出现完整号码。"""
        text = ls.redact("给 13812345678 发送验证码")
        self.assertNotIn("13812345678", text)
        self.assertIn("138", text)
        self.assertIn("5678", text)

    def test_password_style_fields_are_masked(self):
        """形如 键=值 的口令字段要被打码。

        这里刻意用"原文 + 必须消失的那个秘密"成对写，
        而不是拿打码结果去反推 —— 我第一次就是那么写的，
        结果断言把打码结果 "***" 本身当成了"没打码的值"，白白红了一轮。
        """
        for raw, secret in (
            ('password="hunter2000"', "hunter2000"),
            ("DB_PASSWORD=supersecret123", "supersecret123"),
            ("access_token: abcdefghijklm", "abcdefghijklm"),
            ("api_key=zzzzzzzzzzzz", "zzzzzzzzzzzz"),
        ):
            out = ls.redact(raw)
            self.assertNotIn(secret, out, f"秘密没被打码：{raw} → {out}")

    def test_normal_text_is_not_mangled(self):
        """普通文本不能被改坏（脱敏写得太粗暴会让日志完全没法读）。"""
        text = "文档 滤芯说明.txt 已解析完成，共 12 个片段"
        self.assertEqual(ls.redact(text), text)


class RequestIdTestCase(unittest.TestCase):
    def test_new_request_id_is_short_and_unique(self):
        ids = {ls.new_request_id() for _ in range(500)}
        self.assertEqual(len(ids), 500, "请求编号出现重复，日志会串号")
        self.assertTrue(all(len(i) == 12 for i in ids), "编号长度不一致会让日志对不齐")

    def test_set_request_id_defaults_and_passthrough(self):
        ls.set_request_id("given-123")
        self.assertEqual(ls.get_request_id(), "given-123")
        rid = ls.set_request_id("")
        self.assertTrue(rid, "没传编号时要自动生成一个")
        self.assertEqual(ls.get_request_id(), rid)

    def test_filter_injects_request_id_and_pid(self):
        """所有日志（含第三方库打的）都该自动带上编号和进程号。

        抽成 Filter 的意义就在这：不用一处一处改代码去加。
        """
        ls.set_request_id("rid-abc")
        handler = logging.StreamHandler()
        handler.addFilter(ls.RequestIdFilter())
        handler.setFormatter(ls.HumanFormatter())
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        handler.filter(record)
        self.assertEqual(record.request_id, "rid-abc")
        self.assertEqual(record.pid, os.getpid())

    def test_request_id_is_isolated_per_context(self):
        """不同"请求上下文"里的编号不能互相串。

        为什么单测这条：用全局变量存请求编号时，
        并发处理两个请求会出现"张三的日志里带着李四的编号" ——
        这比没有编号更糟，因为它会把排查引向错误的方向。
        """
        import contextvars

        async def worker(name: str) -> str:
            ls.set_request_id(name)
            await asyncio.sleep(0.01)
            return ls.get_request_id()

        async def main():
            return await asyncio.gather(
                *[contextvars.copy_context().run(worker, f"rid-{i}") for i in range(5)]
            )

        got = asyncio.run(main())
        self.assertEqual(sorted(got), [f"rid-{i}" for i in range(5)], f"编号串了：{got}")


class FormatterTestCase(unittest.TestCase):
    def _record(self, msg: str, level: int = logging.INFO, exc=None):
        return logging.LogRecord("kbqa.test", level, __file__, 1, msg, None, exc)

    def test_human_format_contains_key_fields(self):
        ls.set_request_id("rid-1")
        rec = self._record("出错了")
        ls.RequestIdFilter().filter(rec)
        line = ls.HumanFormatter().format(rec)
        self.assertIn("rid-1", line)
        self.assertIn("kbqa.test", line)
        self.assertIn("出错了", line)
        self.assertIn("INFO", line)

    def test_json_format_is_parseable(self):
        ls.set_request_id("rid-2")
        rec = self._record("出错了")
        ls.RequestIdFilter().filter(rec)
        payload = json.loads(ls.JsonFormatter().format(rec))
        for key in ("ts", "level", "logger", "request_id", "pid", "message"):
            self.assertIn(key, payload, f"JSON 日志缺字段 {key}")
        self.assertEqual(payload["request_id"], "rid-2")

    def test_json_keeps_chinese_readable(self):
        """中文不要被转成 \\uXXXX（否则日志系统里看到的是一堆乱码，没法读）。"""
        rec = self._record("文档解析完成")
        ls.RequestIdFilter().filter(rec)
        line = ls.JsonFormatter().format(rec)
        self.assertIn("文档解析完成", line)

    def test_both_formats_redact_secrets(self):
        """**关键**：两种格式都必须在出口脱敏。

        只给其中一种格式加脱敏，是很容易犯的错 ——
        本机开发用 text 格式时看着没问题，一上生产切 JSON 就全泄了。
        """
        rec = self._record("调用失败 key=sk-secretsecretsecret")
        ls.RequestIdFilter().filter(rec)
        for fmt in (ls.HumanFormatter(), ls.JsonFormatter()):
            out = fmt.format(rec)
            self.assertNotIn("secretsecretsecret", out,
                             f"{fmt.__class__.__name__} 没有脱敏，密钥会写进日志")


class SetupLoggingTestCase(unittest.TestCase):
    def tearDown(self):
        # 恢复成默认（避免影响其他测试的日志输出）
        ls.setup_logging(level="INFO", fmt="text")

    def test_setup_is_idempotent(self):
        """重复配置不能叠加 handler。

        不幂等的后果：日志被打印好几遍，而且每调用一次多打一遍 ——
        磁盘很快被刷满，真正的信息也被淹没。
        """
        for _ in range(5):
            ls.setup_logging(level="INFO", fmt="text")
        handlers = [h for h in logging.getLogger().handlers]
        self.assertEqual(len(handlers), 1, f"handler 被叠加了 {len(handlers)} 个")

    def test_switching_format_replaces_handler(self):
        ls.setup_logging(level="DEBUG", fmt="json")
        root = logging.getLogger()
        self.assertEqual(len(root.handlers), 1)
        self.assertIsInstance(root.handlers[0].formatter, ls.JsonFormatter)
        self.assertEqual(root.level, logging.DEBUG)

        ls.setup_logging(level="INFO", fmt="text")
        self.assertIsInstance(root.handlers[0].formatter, ls.HumanFormatter)
        self.assertEqual(root.level, logging.INFO)

    def test_noisy_libraries_are_quieted(self):
        """httpx 这类库太能刷日志，会把有用的信息淹没（本项目日志里 90% 是它）。"""
        ls.setup_logging(level="DEBUG", fmt="text")
        for name in ("httpx", "httpcore", "urllib3"):
            self.assertGreaterEqual(logging.getLogger(name).level, logging.WARNING)


class MiddlewareTestCase(unittest.TestCase):
    """中间件：编号要能在真实 HTTP 请求里生成、带回来、并贯穿日志。"""

    def test_request_id_is_returned_and_used_in_logs(self):
        import httpx

        from app.main import app

        async def t():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r1 = await c.get("/health/live")
                r2 = await c.get("/health/live", headers={"X-Request-ID": "my-own-id"})
                return r1, r2

        r1, r2 = asyncio.run(t())
        self.assertTrue(r1.headers.get("X-Request-ID"), "响应里没带回请求编号，用户报障时无从查起")
        self.assertEqual(len(r1.headers["X-Request-ID"]), 12)
        self.assertEqual(r2.headers.get("X-Request-ID"), "my-own-id",
                         "调用方自带的编号没有被沿用（前后端日志就无法串起来了）")

    def test_different_requests_get_different_ids(self):
        import httpx

        from app.main import app

        async def t():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                ids = []
                for _ in range(5):
                    r = await c.get("/health/live")
                    ids.append(r.headers.get("X-Request-ID"))
                return ids

        ids = asyncio.run(t())
        self.assertEqual(len(set(ids)), 5, "多个请求拿到了同一个编号，日志会混在一起")


if __name__ == "__main__":
    unittest.main()
