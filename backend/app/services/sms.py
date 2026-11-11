"""短信通道：把"发一条验证码短信"这件事抽成一个可以换的实现。

为什么要有这个文件（大白话）：
    验证码短信不是我们自己能发出去的，得花钱找服务商（阿里云、腾讯云、或者第三方短信平台）。
    不同服务商的接口长得完全不一样。如果把这些接口代码直接写进登录逻辑里，
    将来换一家服务商就要动登录代码 —— 那是系统里最不能乱动的地方。

    所以这里做一层"插座"：
        业务代码只说"给这个手机号发验证码 1234"，
        至于到底是用阿里云发、用短信网关发、还是开发时干脆不发（mock），
        由配置决定，业务代码完全不用改。

三条通道：
    mock     —— 测试用。不真发短信，验证码固定 123456 并直接随接口返回，方便本地开发和自动化测试。
    webhook  —— 通用 HTTP 网关。国内很多短信代理商（以及企业自建短信网关）都给一个简单的
                HTTP 接口：把 URL 里的 {phone}/{code} 换掉，请求一下就算发出去。
                这条通道适配面最广，对接成本最低。
    aliyun   —— 阿里云短信（官方 OpenAPI，RPC 风格签名）。已经按官方文档实现，
                但**没有用真账号联调过**（手上没有资质），首次上线需要实测一次。

一条重要纪律（下面 _send 那段会用到）：
    发送**失败**时必须让调用方知道。绝不能"没发出去却告诉用户已发送" ——
    用户傻等短信、点重发被冷却时间挡住，客服电话就打进来了。
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import urllib.parse
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app.core.config import settings

logger = logging.getLogger("kbqa.sms")


@dataclass
class SmsSendResult:
    """一次发送的结果。

    ok=False 时 reason 里要写清原因（会进日志，方便排查"为什么这条没发出去"）。
    test_code 只在不真发的 mock 通道下有值：把它随接口返回给前端，本地开发才方便。
    """

    ok: bool
    reason: str = ""
    test_code: str | None = None


class SmsSender(ABC):
    """短信通道的统一接口。"""

    name: str = "base"

    @abstractmethod
    async def send(self, phone: str, code: str, text: str) -> SmsSendResult:
        """给这个手机号发一条内容为 text 的短信，里面包含验证码 code。"""
        raise NotImplementedError


# ============================================================
# 通道一：mock（测试用，不真发）
# ============================================================


class MockSmsSender(SmsSender):
    """测试通道：什么都不发，把验证码原样交回去。

    为什么要有它：自动化测试不能真的去发短信（要花钱、要联网、结果不可控）。
    另外本地开发时也不可能每次都准备好资质，所以给一个"假装发了"的通道。

    ⚠️ 生产环境用它 = 所有人收到的验证码都是同一串固定值 123456，等于没有验证码。
       所以这里再上一道锁：一旦运行在生产模式，本通道**直接拒绝**（fail closed）。
       为什么不只在启动自检里拦？因为配置是可以被环境变量在运行时改的，
       两道锁总比一道稳（启动自检负责让人"早点知道"，这里负责"绝不真的放行"）。
    """

    name = "mock"

    async def send(self, phone: str, code: str, text: str) -> SmsSendResult:
        if getattr(settings, "is_prod", False):
            logger.error(
                "生产环境试图使用测试短信通道，已拒绝：请配置 SMS_PROVIDER=webhook/aliyun 及其凭据"
            )
            return SmsSendResult(
                ok=False,
                reason="生产环境禁止使用测试短信通道（会把验证码固定成 123456）",
            )
        logger.info("[短信·测试通道] 未真实发送，目标 %s，验证码 %s", phone, code)
        return SmsSendResult(ok=True, reason="测试通道未真实发送", test_code=code)


# ============================================================
# 通道二：通用 HTTP 网关（webhook）
# ============================================================


def render_template(template: str, phone: str, code: str, text: str) -> str:
    """把模板里的占位符替换成实际内容。

    支持的占位符：{phone} 手机号、{code} 验证码、{text} 整条短信内容。
    为什么单独抽成函数：它是纯字符串替换，最容易被写错（比如手机号被 URL 编码了两次），
    单独拿出来才能写测试钉住。
    """
    return (
        (template or "")
        .replace("{phone}", phone)
        .replace("{code}", code)
        .replace("{text}", text)
    )


class WebhookSmsSender(SmsSender):
    """通用 HTTP 网关通道。

    配置里给一个 URL 模板，例如：
        https://sms.example.com/api/send?apikey=xxx&mobile={phone}&content={text}
    我们把它替换好、请求一下，就算把短信交给服务商了。

    为什么这条通道值得做：国内短信代理商有几十家，很多只提供一个简单的 HTTP 接口。
    有了它，换服务商基本只需要改一行配置，不用改代码。
    """

    name = "webhook"

    async def send(self, phone: str, code: str, text: str) -> SmsSendResult:
        url = (getattr(settings, "sms_webhook_url", "") or "").strip()
        if not url:
            return SmsSendResult(ok=False, reason="未配置 SMS_WEBHOOK_URL")

        method = (getattr(settings, "sms_webhook_method", "POST") or "POST").strip().upper()
        timeout = float(getattr(settings, "sms_timeout_seconds", 8.0) or 8.0)

        # 请求头：配置成 JSON 字符串（例如 {"Authorization":"Bearer xxx"}）
        headers: dict[str, str] = {}
        raw_headers = (getattr(settings, "sms_webhook_headers", "") or "").strip()
        if raw_headers:
            try:
                parsed = json.loads(raw_headers)
                if isinstance(parsed, dict):
                    headers = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                # 配置写错不该让整个发送流程崩掉，但必须留下明确日志
                logger.warning("SMS_WEBHOOK_HEADERS 不是合法 JSON，已忽略：%r", raw_headers[:60])

        body_tpl = (getattr(settings, "sms_webhook_body", "") or "").strip()

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == "GET":
                    # GET：参数全放 URL 里
                    target = render_template(url, phone, code, text)
                    resp = await client.get(target, headers=headers)
                else:
                    # POST：URL 里的占位符照样替换；如果没配 body 模板，
                    # 就把手机号/验证码/内容作为表单字段发过去（最常见的一种约定）
                    target = render_template(url, phone, code, text)
                    if body_tpl:
                        payload = render_template(body_tpl, phone, code, text)
                        resp = await client.post(
                            target, content=payload.encode("utf-8"),
                            headers={**headers, "Content-Type": "application/json; charset=utf-8"},
                        )
                    else:
                        resp = await client.post(
                            target, data={"phone": phone, "code": code, "text": text},
                            headers=headers,
                        )
        except httpx.HTTPError as exc:
            logger.warning("短信网关请求失败：%s", exc.__class__.__name__)
            return SmsSendResult(ok=False, reason=f"请求短信网关失败（{exc.__class__.__name__}）")

        if 200 <= resp.status_code < 300:
            return SmsSendResult(ok=True, reason=f"网关返回 {resp.status_code}")
        # 注意：这里只记状态码，不把响应体整段打进日志（可能含服务商的密钥回显）
        logger.warning("短信网关返回非成功状态码：%s", resp.status_code)
        return SmsSendResult(ok=False, reason=f"短信网关返回 {resp.status_code}")


# ============================================================
# 通道三：阿里云短信（官方 OpenAPI）
# ============================================================


def _aliyun_percent_encode(value: str) -> str:
    """阿里云签名要求的转义方式。

    比标准 URL 编码多三条替换规则（这是官方文档里写死的，不这么做签名一定对不上）：
        + 要变成 %20（标准编码会输出 +，但阿里云不认）
        * 要变成 %2A
        %7E 要还原成 ~
    """
    encoded = urllib.parse.quote(str(value), safe="")
    return encoded.replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


def build_aliyun_signature(params: dict[str, str], access_key_secret: str, method: str = "GET") -> str:
    """按阿里云官方算法算出请求签名。

    签名的作用（大白话）：证明"这个请求确实是我们发的"。
    服务商和我们都用同一个密钥、对同一段内容做一次"加密指纹"，
    两边算出来的指纹一样，就说明请求没被篡改、也不是别人冒充的。
    密钥本身不上网，所以别人截获了请求也伪造不出来。
    """
    # 1) 参数按 key 的字典序排好，拼成 k=v&k=v 的样子
    canonical = "&".join(
        f"{_aliyun_percent_encode(k)}={_aliyun_percent_encode(v)}"
        for k, v in sorted(params.items())
    )
    # 2) 待签名串 = 请求方法 & 路径(编码后的 /) & 参数串
    string_to_sign = f"{method}&{_aliyun_percent_encode('/')}&{_aliyun_percent_encode(canonical)}"
    # 3) 用"密钥 + 一个 &" 作为 HMAC-SHA1 的钥匙做一次指纹，再 base64
    digest = hmac.new(
        (access_key_secret + "&").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


class AliyunSmsSender(SmsSender):
    """阿里云短信通道（SendSms 接口）。

    只依赖标准库 + httpx，没有引入阿里云官方 SDK：
      · 官方 SDK 体积大、版本更新频繁，而我们只用到这一个接口；
      · 签名算法是公开文档，几十行就能实现，自己实现反而更好排查。
    ⚠️ 本实现按官方文档编写，但**未用真实账号联调过**（手上没有短信资质）。
       首次上线要用真实号码实测一条，确认签名与模板参数无误。
    """

    name = "aliyun"

    ENDPOINT = "https://dysmsapi.aliyuncs.com/"

    async def send(self, phone: str, code: str, text: str) -> SmsSendResult:
        key_id = (getattr(settings, "sms_aliyun_access_key_id", "") or "").strip()
        key_secret = (getattr(settings, "sms_aliyun_access_key_secret", "") or "").strip()
        sign_name = (getattr(settings, "sms_aliyun_sign_name", "") or "").strip()
        template = (getattr(settings, "sms_aliyun_template_code", "") or "").strip()
        region = (getattr(settings, "sms_aliyun_region", "cn-hangzhou") or "cn-hangzhou").strip()
        timeout = float(getattr(settings, "sms_timeout_seconds", 8.0) or 8.0)

        # 缺任何一项都发不出去。这里提前说清楚缺哪个，比让服务商返回一个
        # "SignatureDoesNotMatch" 再去猜要省事得多。
        missing = [
            label for label, value in (
                ("SMS_ALIYUN_ACCESS_KEY_ID", key_id),
                ("SMS_ALIYUN_ACCESS_KEY_SECRET", key_secret),
                ("SMS_ALIYUN_SIGN_NAME", sign_name),
                ("SMS_ALIYUN_TEMPLATE_CODE", template),
            ) if not value
        ]
        if missing:
            return SmsSendResult(ok=False, reason="阿里云短信配置不完整：" + "、".join(missing))

        params = {
            "AccessKeyId": key_id,
            "Action": "SendSms",
            "Format": "JSON",
            "PhoneNumbers": phone,
            "RegionId": region,
            "SignName": sign_name,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": uuid.uuid4().hex,   # 防重放：每次请求都换一个随机串
            "SignatureVersion": "1.0",
            "TemplateCode": template,
            # 模板变量：阿里云短信模板里写的是 ${code}，这里对应填上
            "TemplateParam": json.dumps({"code": code}, ensure_ascii=False),
            "Timestamp": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Version": "2017-05-25",
        }
        params["Signature"] = build_aliyun_signature(params, key_secret)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(self.ENDPOINT, params=params)
        except httpx.HTTPError as exc:
            logger.warning("阿里云短信请求失败：%s", exc.__class__.__name__)
            return SmsSendResult(ok=False, reason=f"请求阿里云短信失败（{exc.__class__.__name__}）")

        try:
            data = resp.json()
        except ValueError:
            logger.warning("阿里云短信返回了非 JSON 内容（HTTP %s）", resp.status_code)
            return SmsSendResult(ok=False, reason=f"阿里云短信返回异常（HTTP {resp.status_code}）")

        # 注意：阿里云的失败也是 HTTP 200，要靠 Code 字段判断，
        # 只看状态码会把"签名错、模板没审核过、余额不足"一律当成发送成功。
        if str(data.get("Code", "")).upper() == "OK":
            return SmsSendResult(ok=True, reason="阿里云已受理")
        biz_code = str(data.get("Code", "")) or "未知"
        logger.warning("阿里云短信业务失败 Code=%s Message=%s", biz_code, data.get("Message"))
        return SmsSendResult(ok=False, reason=f"阿里云短信失败：{biz_code}")


# ============================================================
# 选通道
# ============================================================

_SENDERS: dict[str, type[SmsSender]] = {
    "mock": MockSmsSender,
    "webhook": WebhookSmsSender,
    "aliyun": AliyunSmsSender,
}


def available_providers() -> list[str]:
    """当前代码支持哪些通道（给运维/配置检查用）。"""
    return sorted(_SENDERS)


def get_sms_sender(provider: str | None = None) -> SmsSender:
    """按配置挑一条通道。

    配置写了一个不认识的通道名时**不要**默默退化成 mock ——
    那样生产环境会变成"所有人验证码都是 123456"，而且不会有任何报错。
    这里的做法是明确抛错，让问题在启动/首次调用时立刻暴露。
    """
    name = (provider or getattr(settings, "sms_provider", "mock") or "mock").strip().lower()
    cls = _SENDERS.get(name)
    if cls is None:
        raise ValueError(
            f"不认识的短信通道 SMS_PROVIDER={name!r}，可选：{'、'.join(available_providers())}"
        )
    return cls()


def build_code_text(code: str, ttl_minutes: int) -> str:
    """短信正文。抽出来是为了让通道间口径一致，也便于以后做多语言。"""
    return f"您的验证码是 {code}，{ttl_minutes} 分钟内有效，请勿转发给他人。"
