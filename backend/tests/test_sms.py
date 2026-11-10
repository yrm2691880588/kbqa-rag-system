"""短信通道测试（P1-H）。

这个文件在测什么（大白话）：
    验证码短信得靠服务商（阿里云、短信网关）发出去。这个文件验证三件事：

    ① **消息内容拼对了**：手机号、验证码真的进了要发出去的报文里。
       拼错了的后果很隐蔽 —— 短信"发送成功"，但用户收到的是一条没有验证码的短信。

    ② **选通道选对了**：配置写 mock 就用 mock、写 webhook 就用 webhook、
       写了个不认识的名字要**报错**，而不是默默退化成 mock
       （默默退化的后果是：生产环境所有人验证码都是 123456）。

    ③ **发不出去时不能骗人**：发送失败必须把刚存下的验证码删掉。
       不删的话用户看到"发送失败"，重试却被 60 秒冷却挡住，
       而且库里留了一条谁也不知道的验证码。

用什么手段验证"真发出去了"：本地起一个**真的 HTTP 小服务**当作短信网关，
把请求真的发过去，再检查它到底收到了什么。比 mock 掉网络调用可信得多
（mock 掉的话，参数名写错了测试照样通过）。

运行方式（backend 目录下）：
    python -m unittest tests.test_sms -v
"""

import asyncio
import base64
import contextlib
import http.server
import os
import sys
import threading
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 本机自测要绕过系统代理：这台机器给子进程设了 http_proxy，
# 不设置的话连自己起在 127.0.0.1 上的小服务都会被塞进代理，直接连不通。
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core import startup_check as sc
from app.db.base import Base
from app.models.verification_code import VerificationCode
from app.services import auth_guard, sms

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 这些配置项在测试里会被临时改来改去，跑完必须还原，否则会污染其他测试
_MUTABLE = [
    "app_env", "sms_provider", "sms_timeout_seconds",
    "sms_webhook_url", "sms_webhook_method", "sms_webhook_body", "sms_webhook_headers",
    "sms_aliyun_access_key_id", "sms_aliyun_access_key_secret",
    "sms_aliyun_sign_name", "sms_aliyun_template_code", "sms_aliyun_region",
]


def _run(coro):
    return asyncio.run(coro)


@contextlib.contextmanager
def temp_settings(**kwargs):
    """临时改配置，退出时原样还原（哪怕中途抛异常也还原）。"""
    old = {k: getattr(settings, k) for k in _MUTABLE if hasattr(settings, k)}
    try:
        for k, v in kwargs.items():
            setattr(settings, k, v)
        yield settings
    finally:
        for k, v in old.items():
            setattr(settings, k, v)


class _Handler(http.server.BaseHTTPRequestHandler):
    """迷你短信网关：把收到的请求原样记下来，供测试检查。"""

    def _record_and_reply(self, body: bytes = b"") -> None:
        self.server.records.append({                # type: ignore[attr-defined]
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        self.send_response(self.server.status)       # type: ignore[attr-defined]
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):                                # noqa: N802
        self._record_and_reply()

    def do_POST(self):                               # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._record_and_reply(body)

    def log_message(self, *args):                    # 静音：测试输出保持干净
        pass


class StubGateway:
    """一个真的在本地监听的 HTTP 小服务（真端口、真请求）。"""

    def __init__(self, status: int = 200):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.records = []      # type: ignore[attr-defined]
        self.server.status = status   # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        return False

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def records(self) -> list[dict]:
        return self.server.records  # type: ignore[attr-defined]


# ============================================================
# 一、纯函数：模板替换 / 短信正文 / 阿里云签名的转义规则
# ============================================================


class HelperTestCase(unittest.TestCase):
    def test_render_template_replaces_placeholders(self):
        """URL 模板里的三个占位符都要被换掉。"""
        got = sms.render_template(
            "https://g/send?m={phone}&c={code}&t={text}", "13800000000", "246810", "您的验证码是 246810"
        )
        self.assertIn("13800000000", got)
        self.assertIn("246810", got)
        self.assertIn("您的验证码是 246810", got)
        # 换完之后不该还留着占位符
        self.assertNotIn("{phone}", got)
        self.assertNotIn("{code}", got)
        self.assertNotIn("{text}", got)

    def test_render_template_without_placeholder_is_unchanged(self):
        """没有占位符的模板原样返回（别自作聪明乱改）。"""
        self.assertEqual(sms.render_template("https://g/send", "1", "2", "3"), "https://g/send")

    def test_build_code_text(self):
        """短信正文里必须同时有验证码和有效期（用户要知道它多久失效）。"""
        text = sms.build_code_text("135790", 5)
        self.assertIn("135790", text)
        self.assertIn("5", text)

    def test_aliyun_percent_encode_special_rules(self):
        """阿里云签名的转义有三条与标准 URL 编码不同的规则，最容易写错。

        写错的后果：签名永远对不上，短信一条都发不出去，报错还是
        "SignatureDoesNotMatch"，根本看不出是编码问题。
        """
        enc = sms._aliyun_percent_encode
        self.assertEqual(enc(" "), "%20", "空格必须编成 %20，不能编成 +")
        self.assertEqual(enc("*"), "%2A")
        self.assertEqual(enc("~"), "~", "波浪号要还原回 ~，不能编成 %7E")
        self.assertEqual(enc("/"), "%2F")
        self.assertEqual(enc("a+b"), "a%2Bb")
        self.assertEqual(enc("="), "%3D")

    def test_aliyun_signature_is_deterministic_and_key_sensitive(self):
        """签名：同输入必同输出；密钥一换签名必变（否则等于没签名）。"""
        params = {
            "Action": "SendSms", "PhoneNumbers": "13800000000",
            "SignatureNonce": "fixed-nonce", "Timestamp": "2026-10-08T00:00:00Z",
        }
        a = sms.build_aliyun_signature(params, "secret-1")
        b = sms.build_aliyun_signature(params, "secret-1")
        c = sms.build_aliyun_signature(params, "secret-2")
        self.assertEqual(a, b, "同样的输入算出了不同签名，服务商侧必然验签失败")
        self.assertNotEqual(a, c, "换了密钥签名却没变，等于没有签名")
        # HMAC-SHA1 的摘要固定 20 字节，base64 之后应是 28 个字符
        raw = base64.b64decode(a)
        self.assertEqual(len(raw), 20)
        self.assertEqual(len(a), 28)


# ============================================================
# 二、选通道
# ============================================================


class ProviderSelectionTestCase(unittest.TestCase):
    def test_available_providers(self):
        self.assertEqual(sms.available_providers(), ["aliyun", "mock", "webhook"])

    def test_select_by_name(self):
        with temp_settings(sms_provider="mock"):
            self.assertIsInstance(sms.get_sms_sender(), sms.MockSmsSender)
        with temp_settings(sms_provider="WEBHOOK"):     # 大小写不敏感
            self.assertIsInstance(sms.get_sms_sender(), sms.WebhookSmsSender)
        with temp_settings(sms_provider="aliyun"):
            self.assertIsInstance(sms.get_sms_sender(), sms.AliyunSmsSender)

    def test_unknown_provider_raises(self):
        """不认识的名字要**报错**，不能默默退化成 mock。

        默默退化是这条测试真正防的事故：生产环境配置写错一个字母，
        系统照常启动、照常"发送成功"，而所有人收到的验证码都是 123456。
        """
        with temp_settings(sms_provider="tengxun"):
            with self.assertRaises(ValueError) as ctx:
                sms.get_sms_sender()
            self.assertIn("tengxun", str(ctx.exception))
            self.assertIn("webhook", str(ctx.exception))   # 告诉运维可选值


# ============================================================
# 三、mock 通道：开发可用，生产必须拒绝
# ============================================================


class MockSenderTestCase(unittest.TestCase):
    def test_works_in_dev(self):
        with temp_settings(app_env="dev", sms_provider="mock"):
            r = _run(sms.MockSmsSender().send("13800000000", "246810", "随便什么内容"))
        self.assertTrue(r.ok)
        self.assertEqual(r.test_code, "246810", "开发环境应把测试码交回去，方便本地调试")

    def test_refuses_in_prod(self):
        """**关键**：生产环境用 mock 通道必须直接失败。

        这是"万一启动自检被绕过"时的第二道锁。
        如果这条不生效，任何人在登录页输入任意手机号 + 123456 就能登录，
        而且系统运行得"完全正常"，不会有任何报错。
        """
        with temp_settings(app_env="prod", sms_provider="mock"):
            r = _run(sms.MockSmsSender().send("13800000000", "246810", "内容"))
        self.assertFalse(r.ok, "生产环境居然用 mock 通道发出了验证码")
        self.assertIsNone(r.test_code)
        self.assertIn("生产环境", r.reason)


# ============================================================
# 四、webhook 通道：真的发一次，看网关收到了什么
# ============================================================


class WebhookSenderTestCase(unittest.TestCase):
    def test_get_request_carries_phone_and_code(self):
        with StubGateway() as gw:
            with temp_settings(
                app_env="dev",
                sms_provider="webhook",
                sms_webhook_method="GET",
                sms_webhook_url=(
                    f"http://127.0.0.1:{gw.port}/send"
                    "?mobile={phone}&code={code}&content={text}"
                ),
            ):
                r = _run(sms.WebhookSmsSender().send(
                    "13800000000", "246810", "您的验证码是 246810"))

            self.assertTrue(r.ok, f"发送失败：{r.reason}")
            self.assertEqual(len(gw.records), 1, "网关没收到请求")
            rec = gw.records[0]
            self.assertEqual(rec["method"], "GET")
            query = urllib.parse.parse_qs(urllib.parse.urlparse(rec["path"]).query)
            self.assertEqual(query["mobile"], ["13800000000"])
            self.assertEqual(query["code"], ["246810"])
            self.assertIn("246810", query["content"][0])

    def test_post_form_default(self):
        """POST 且没给请求体模板时，按 phone/code/text 三个表单字段发。"""
        with StubGateway() as gw:
            with temp_settings(
                app_env="dev", sms_provider="webhook", sms_webhook_method="POST",
                sms_webhook_body="",
                sms_webhook_url=f"http://127.0.0.1:{gw.port}/send",
            ):
                r = _run(sms.WebhookSmsSender().send("13900000000", "135790", "内容"))
            self.assertTrue(r.ok, r.reason)
            body = gw.records[0]["body"].decode("utf-8")
            self.assertIn("13900000000", body)
            self.assertIn("135790", body)

    def test_post_custom_json_body(self):
        """给了请求体模板就用模板（很多网关要求 JSON 格式）。"""
        with StubGateway() as gw:
            with temp_settings(
                app_env="dev", sms_provider="webhook", sms_webhook_method="POST",
                sms_webhook_body='{"to":"{phone}","code":"{code}"}',
                sms_webhook_url=f"http://127.0.0.1:{gw.port}/send",
            ):
                r = _run(sms.WebhookSmsSender().send("13700000000", "112233", "内容"))
            self.assertTrue(r.ok, r.reason)
            body = gw.records[0]["body"].decode("utf-8")
            self.assertEqual(body, '{"to":"13700000000","code":"112233"}')

    def test_extra_headers_sent(self):
        """网关要求密钥放请求头时，配的头必须真的带过去。"""
        with StubGateway() as gw:
            with temp_settings(
                app_env="dev", sms_provider="webhook", sms_webhook_method="GET",
                sms_webhook_headers='{"Authorization":"Bearer tok-123"}',
                sms_webhook_url=f"http://127.0.0.1:{gw.port}/send?m={{phone}}",
            ):
                r = _run(sms.WebhookSmsSender().send("13600000000", "445566", "内容"))
            self.assertTrue(r.ok, r.reason)
            self.assertEqual(gw.records[0]["headers"].get("Authorization"), "Bearer tok-123")

    def test_non_2xx_is_failure(self):
        """网关返回 500 时必须判为失败（不能"报出去就算成功"）。"""
        with StubGateway(status=500) as gw:
            with temp_settings(
                app_env="dev", sms_provider="webhook", sms_webhook_method="GET",
                sms_webhook_url=f"http://127.0.0.1:{gw.port}/send?m={{phone}}",
            ):
                r = _run(sms.WebhookSmsSender().send("13800000000", "246810", "内容"))
        self.assertFalse(r.ok)
        self.assertIn("500", r.reason)

    def test_unreachable_gateway_is_failure_not_crash(self):
        """网关连不上时要返回"失败"，不能让异常直接冒到接口层变成 500。"""
        with temp_settings(
            app_env="dev", sms_provider="webhook", sms_webhook_method="GET",
            sms_webhook_url="http://127.0.0.1:1/send?m={phone}",  # 这个端口上没人监听
            sms_timeout_seconds=2.0,
        ):
            r = _run(sms.WebhookSmsSender().send("13800000000", "246810", "内容"))
        self.assertFalse(r.ok)
        self.assertIn("失败", r.reason)

    def test_missing_url_is_failure(self):
        with temp_settings(app_env="dev", sms_provider="webhook", sms_webhook_url=""):
            r = _run(sms.WebhookSmsSender().send("13800000000", "246810", "内容"))
        self.assertFalse(r.ok)
        self.assertIn("SMS_WEBHOOK_URL", r.reason)


# ============================================================
# 五、阿里云通道：配置不全要明确报出缺哪个
# ============================================================


class AliyunSenderTestCase(unittest.TestCase):
    def test_missing_config_reports_exactly_what_is_missing(self):
        """配置不全时要一项一项点出来。

        为什么在意：阿里云的报错是 "SignatureDoesNotMatch"、"isv.SMS_SIGNATURE_ILLEGAL"
        这类英文短码，运维看到根本不知道是"签名没报备"还是"密钥填错了"。
        我们自己提前把缺的项列清楚，能省掉一轮排查。
        """
        with temp_settings(
            app_env="dev", sms_provider="aliyun",
            sms_aliyun_access_key_id="", sms_aliyun_access_key_secret="",
            sms_aliyun_sign_name="", sms_aliyun_template_code="",
        ):
            r = _run(sms.AliyunSmsSender().send("13800000000", "246810", "内容"))
        self.assertFalse(r.ok)
        for label in ("SMS_ALIYUN_ACCESS_KEY_ID", "SMS_ALIYUN_ACCESS_KEY_SECRET",
                      "SMS_ALIYUN_SIGN_NAME", "SMS_ALIYUN_TEMPLATE_CODE"):
            self.assertIn(label, r.reason)

    def test_partial_config_lists_only_the_missing_one(self):
        with temp_settings(
            app_env="dev", sms_provider="aliyun",
            sms_aliyun_access_key_id="id", sms_aliyun_access_key_secret="sec",
            sms_aliyun_sign_name="签名", sms_aliyun_template_code="",
        ):
            r = _run(sms.AliyunSmsSender().send("13800000000", "246810", "内容"))
        self.assertFalse(r.ok)
        self.assertIn("SMS_ALIYUN_TEMPLATE_CODE", r.reason)
        self.assertNotIn("SMS_ALIYUN_SIGN_NAME", r.reason)


# ============================================================
# 六、串到发码流程里：失败必须回滚，不能白占用户 60 秒
# ============================================================


class IssueSmsCodeTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        async def _create():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        _run(_create())

    def tearDown(self):
        _run(self.engine.dispose())

    async def _rows(self) -> list[VerificationCode]:
        async with self.Session() as db:
            return list((await db.scalars(select(VerificationCode))).all())

    def test_dev_mock_path_returns_test_code(self):
        async def t():
            async with self.Session() as db:
                with temp_settings(app_env="dev", sms_provider="mock"):
                    ok, msg, code = await auth_guard.issue_sms_code(db, "13800000000", "1.1.1.1")
            self.assertTrue(ok, msg)
            self.assertEqual(code, "123456")
            rows = await self._rows()
            self.assertEqual(len(rows), 1, "应该留下一条验证码记录")
        _run(t())

    def test_real_channel_success_records_code_and_hides_it(self):
        """真实通道发成功：记录落库，但**不**把验证码交回给调用方（不然等于没验证）。"""
        async def t():
            with StubGateway() as gw:
                async with self.Session() as db:
                    with temp_settings(
                        app_env="dev", sms_provider="webhook", sms_webhook_method="GET",
                        sms_webhook_url=f"http://127.0.0.1:{gw.port}/s?m={{phone}}&c={{code}}",
                    ):
                        ok, msg, code = await auth_guard.issue_sms_code(db, "13800000000", "1.1.1.1")
                self.assertTrue(ok, msg)
                self.assertIsNone(code, "真实通道不该把验证码回传给前端")
                self.assertEqual(len(gw.records), 1)
                # 真正发出去的那个验证码，必须是六位数字串
                sent = urllib.parse.parse_qs(
                    urllib.parse.urlparse(gw.records[0]["path"]).query)["c"][0]
                self.assertRegex(sent, r"^\d{6}$")
            rows = await self._rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].code_hash, auth_guard._sha256(sent),
                             "库里存的哈希与实际发出去的验证码对不上")
        _run(t())

    def test_send_failure_rolls_back_so_user_can_retry(self):
        """**关键**：发送失败要把验证码记录删掉。

        不删的后果（都是真的会发生）：
          ① 用户看到"发送失败"，想重试 → 被 60 秒冷却挡住 → 只能干等或找客服；
          ② 库里留下一条谁都不知道的验证码，"今天发了几条"的统计也不准。
        """
        async def t():
            async with self.Session() as db:
                with temp_settings(
                    app_env="dev", sms_provider="webhook", sms_webhook_method="GET",
                    sms_webhook_url="http://127.0.0.1:1/s?m={phone}",   # 没人监听 → 必然失败
                    sms_timeout_seconds=2.0,
                ):
                    ok, msg, code = await auth_guard.issue_sms_code(db, "13800000000", "1.1.1.1")
            self.assertFalse(ok, "发送失败了却告诉用户成功")
            self.assertIsNone(code)
            self.assertEqual(await self._rows(), [], "发送失败却把验证码留在了库里（用户会被冷却时间白挡）")

            # 立刻重试必须能再发（冷却时间不该被失败的那次占掉）
            with StubGateway() as gw:
                async with self.Session() as db:
                    with temp_settings(
                        app_env="dev", sms_provider="webhook", sms_webhook_method="GET",
                        sms_webhook_url=f"http://127.0.0.1:{gw.port}/s?m={{phone}}&c={{code}}",
                    ):
                        ok2, msg2, _ = await auth_guard.issue_sms_code(db, "13800000000", "1.1.1.1")
            self.assertTrue(ok2, f"失败之后立刻重试被拒了：{msg2}")
        _run(t())

    def test_unknown_provider_does_not_crash_but_fails_cleanly(self):
        """配置写错通道名时：接口不该 500，应返回"发送失败"并留下明确日志。"""
        async def t():
            async with self.Session() as db:
                with temp_settings(app_env="dev", sms_provider="tengxun"):
                    ok, msg, code = await auth_guard.issue_sms_code(db, "13800000000", "1.1.1.1")
            self.assertFalse(ok)
            self.assertIn("失败", msg)
            self.assertEqual(await self._rows(), [])
        _run(t())

    def test_prod_with_mock_channel_cannot_issue_any_code(self):
        """生产环境 + mock 通道：发不出验证码（等于手机号登录整体关闭）。

        这是"配置没接好就别对外"的兜底：宁可这个功能不可用，
        也不能让所有人用 123456 登进别人的账号。
        """
        async def t():
            async with self.Session() as db:
                with temp_settings(app_env="prod", sms_provider="mock"):
                    ok, msg, code = await auth_guard.issue_sms_code(db, "13800000000", "1.1.1.1")
            self.assertFalse(ok)
            self.assertIsNone(code, "生产环境把固定验证码 123456 交回去了")
            self.assertEqual(await self._rows(), [])
        _run(t())


# ============================================================
# 七、启动自检：配置没接好就不许启动
# ============================================================


class StartupCheckSmsTestCase(unittest.TestCase):
    """直接调用纯函数收集问题清单，不起服务。

    ⚠️ 这里断言的是**告警**而不是"拒绝启动"：
       手机号登录只是可选入口，有些私有部署根本没有短信资质，
       因为这一项没配好就让整站起不来不划算。
       真正的安全由发送那一刻兜底（见 MockSenderTestCase / IssueSmsCodeTestCase）——
       生产环境用测试通道时**发不出任何验证码**。
       下面的断言因此分成两层：
         ① 启动时要"说出来"（告警里有这条）；
         ② 真发的时候要"发不出去"（另一组测试负责）。
    """

    def _issues(self, **kwargs):
        cfg = sc.Settings()
        # 本用例只关心短信通道的启动检查，与压测用的 LLM_MOCK 开关无关。
        # 但 CI 环境可能设置了 LLM_MOCK=true（作为"禁止真调大模型"的保险），
        # 而它在生产环境下会额外产生一条 error，混进下面"不该有 mock 相关 error"
        # 的断言里造成假失败。这里显式关掉，让用例只验证自己想验证的那件事。
        cfg.llm_mock = False
        for k, v in kwargs.items():
            setattr(cfg, k, v)
        return sc.collect_startup_issues(cfg)

    def test_prod_mock_warns_loudly(self):
        errors, warnings = self._issues(
            app_env="prod", secret_key="x" * 40, llm_api_key="k", embedding_api_key="k",
            sms_provider="mock",
        )
        self.assertTrue(any("mock" in w and "123456" in w for w in warnings),
                        f"生产环境用 mock 短信通道时没有任何提示：warnings={warnings}")
        self.assertFalse(any("mock" in e.lower() for e in errors),
                         "短信只该告警、不该拦下整站启动")

    def test_dev_mock_is_silent(self):
        """开发环境用 mock 是正常用法，连提示都不该有（否则日志会很吵）。"""
        _, warnings = self._issues(app_env="dev", sms_provider="mock")
        self.assertFalse(any("mock" in w for w in warnings), str(warnings))

    def test_prod_webhook_without_url_warns(self):
        _, warnings = self._issues(
            app_env="prod", secret_key="x" * 40, llm_api_key="k", embedding_api_key="k",
            sms_provider="webhook", sms_webhook_url="",
        )
        self.assertTrue(any("SMS_WEBHOOK_URL" in w for w in warnings), str(warnings))

    def test_prod_aliyun_missing_credentials_warns(self):
        _, warnings = self._issues(
            app_env="prod", secret_key="x" * 40, llm_api_key="k", embedding_api_key="k",
            sms_provider="aliyun", sms_aliyun_access_key_id="",
        )
        self.assertTrue(any("SMS_ALIYUN_ACCESS_KEY_ID" in w for w in warnings), str(warnings))

    def test_prod_fully_configured_webhook_is_quiet(self):
        errors, warnings = self._issues(
            app_env="prod", secret_key="x" * 40, llm_api_key="k", embedding_api_key="k",
            database_url="postgresql+asyncpg://u:p@postgres:5432/kbqa",
            sms_provider="webhook",
            sms_webhook_url="https://g/send?m={phone}&c={code}",
        )
        self.assertEqual([w for w in warnings if "短信" in w or "SMS" in w], [])
        self.assertEqual([e for e in errors if "SMS" in e], [])

    def test_unknown_provider_warns(self):
        _, warnings = self._issues(app_env="dev", sms_provider="tengxun")
        self.assertTrue(any("tengxun" in w for w in warnings), str(warnings))

    def test_webhook_url_without_placeholders_warns(self):
        """URL 里既没有手机号也没有内容 → 提醒（短信发给谁、发什么都说不清）。"""
        _, warnings = self._issues(
            app_env="dev", sms_provider="webhook", sms_webhook_url="https://g/send",
        )
        self.assertTrue(any("占位符" in w for w in warnings), str(warnings))


# ============================================================
# 八、配置模板：写了但程序不认的键 = 不会生效的配置
# ============================================================


class EnvTemplateTestCase(unittest.TestCase):
    """把 .env 模板里的每个配置键都拿去和程序真正认识的清单对一遍。

    为什么值得一条测试：配置写错名字，程序**不会报错**，只是不认识、默默用默认值。
    开发时往往看不出来（默认值恰好一样），部署到服务器上就变成
    "我明明改了配置，怎么没生效？" 的疑难杂症。这条测试专门防这一类。
    """

    # 这些键属于 docker compose / 初始化脚本，不属于后端程序
    ALLOWED_EXTRA = {
        "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_PORT",
        "QDRANT_PORT", "BACKEND_PORT", "FRONTEND_PORT", "PIP_INDEX_URL",
        "NPM_REGISTRY", "NGINX_PORT", "COMPOSE_PROJECT_NAME", "DOCKER_IMAGE_PREFIX",
    }

    def _check(self, filename: str) -> tuple[int, list[str]]:
        import pathlib
        path = pathlib.Path(BACKEND) / filename
        if not path.exists():
            self.skipTest(f"{filename} 不存在")
        known = set(sc.Settings.model_fields.keys())
        total, unknown = 0, []
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            total += 1
            key = s.split("=", 1)[0].strip()
            if key.lower() not in known and key not in self.ALLOWED_EXTRA:
                unknown.append(key)
        return total, unknown

    def test_prod_template_keys_are_all_recognized(self):
        total, unknown = self._check(".env.prod.example")
        self.assertGreater(total, 20, "模板文件好像没读到内容")
        self.assertEqual(unknown, [], f"这些配置键程序不认识，写了也不会生效：{unknown}")

    def test_dev_template_keys_are_all_recognized(self):
        _, unknown = self._check(".env.example")
        self.assertEqual(unknown, [], f"这些配置键程序不认识，写了也不会生效：{unknown}")


if __name__ == "__main__":
    unittest.main()
