"""登录守卫服务：图形验证码 / 短信验证码 / 登录失败锁定。

一个文件收拢所有"登录安全"相关逻辑，auth.py 只负责调用，保持接口层清爽。

设计要点：
- 验证码答案只存 sha256 哈希（DB 泄露也看不到原文）
- 所有限流/计数都落在数据库 → 后端多进程部署时统计依然全局一致
- 时间判断统一取回后在 Python 层比较（规避 PG/SQLite 时区存储差异）
- 短信 mock 模式（sms_provider=mock）不真发短信，固定验证码 123456，方便本地开发与测试
"""

import base64
import datetime as dt
import hashlib
import io
import logging
import random
import re
import string
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.login_failure import LoginFailure
from app.models.verification_code import VerificationCode

logger = logging.getLogger("kbqa.auth")

# ============ 手机号规则 ============
# 中国大陆手机号：1 开头、第二位 3-9、共 11 位
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

# 图形验证码字符集：去掉易混淆的 0/O/1/l/I
_CAPTCHA_CHARS = string.digits + string.ascii_uppercase
_CAPTCHA_CHARS = _CAPTCHA_CHARS.replace("0", "").replace("1", "").replace("O", "").replace("I", "")


def is_valid_phone(phone: str) -> bool:
    """校验手机号格式（中国大陆 11 位）。"""
    return bool(_PHONE_RE.match(phone or ""))


def _sha256(text: str) -> str:
    """单向哈希，用于存验证码答案。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utcnow() -> dt.datetime:
    """当前 UTC 时间（带时区）。"""
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value: dt.datetime) -> dt.datetime:
    """数据库取回的时间可能没带时区（SQLite 就是这样），统一补成 UTC 再比较。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def _recent_codes(db_result_rows: list) -> list:
    """把数据库取回的验证码行转成 (时间, 行) 列表，时间已归一可比。"""
    return [(r.created_at, r) for r in db_result_rows]


# ============================================================
# 图形验证码（人机校验：注册 / 发短信前必须答对）
# ============================================================

def _load_font(size: int):
    """加载一个可用字体：优先系统常见字体，找不到用内置字体。"""
    from PIL import ImageFont

    candidates = [
        "C:/Windows/Fonts/arial.ttf",                      # Windows
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Ubuntu/Debian
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",           # 部分发行版路径
        "/System/Library/Fonts/Helvetica.ttc",              # macOS
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow>=10.1 支持指定字号
    except TypeError:
        return ImageFont.load_default()


def _generate_captcha_text(length: int = 4) -> str:
    """生成 4 位随机验证码文本（不含易混淆字符）。"""
    return "".join(random.choices(_CAPTCHA_CHARS, k=length))


def _render_captcha_image(text: str) -> str:
    """把验证码画成一张带干扰的图片，返回 data:image/png;base64 字符串。

    防脚本思路：扭曲字符 + 干扰线 + 噪点 → 简单 OCR 脚本难以识别。
    """
    from PIL import Image, ImageDraw

    width, height = 150, 52
    image = Image.new("RGB", (width, height), (245, 248, 252))
    draw = ImageDraw.Draw(image)
    font = _load_font(34)

    # 干扰线（4 条随机斜线）
    for _ in range(4):
        x1, y1 = random.randint(0, width // 2), random.randint(0, height)
        x2, y2 = random.randint(width // 2, width), random.randint(0, height)
        draw.line((x1, y1, x2, y2), fill=(random.randint(150, 210),) * 3, width=2)

    # 逐字符绘制，每个字符独立颜色、上下错位
    x = 18
    for ch in text:
        color = (random.randint(30, 120), random.randint(30, 120), random.randint(30, 120))
        draw.text((x, random.randint(2, 12)), ch, font=font, fill=color)
        x += 30

    # 噪点（随机小圆点）
    for _ in range(120):
        draw.point(
            (random.randint(0, width), random.randint(0, height)),
            fill=(random.randint(120, 200),) * 3,
        )

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


async def issue_captcha(db: AsyncSession) -> tuple[str, str]:
    """生成一张图形验证码并登记到库。

    返回 (captcha_id, image_base64)。captcha_id 是取码凭证，答案只以哈希入库。
    """
    text = _generate_captcha_text()
    captcha_id = uuid.uuid4().hex
    db.add(
        VerificationCode(
            kind="captcha",
            target=captcha_id,
            code_hash=_sha256(text.upper()),
            expires_at=_utcnow() + dt.timedelta(minutes=settings.captcha_ttl_minutes),
        )
    )
    await db.commit()
    return captcha_id, _render_captcha_image(text)


async def consume_captcha(db: AsyncSession, captcha_id: str, answer: str) -> bool:
    """校验图形验证码答案，正确则立即作废（一次性）。

    任何异常（不存在/已用/过期/答错）统一返回 False，不暴露细节。
    """
    if not captcha_id or not answer:
        return False
    row = await db.scalar(
        select(VerificationCode).where(
            VerificationCode.kind == "captcha", VerificationCode.target == captcha_id
        )
    )
    if row is None or row.used or _as_utc(row.expires_at) < _utcnow():
        return False
    if row.code_hash != _sha256(answer.strip().upper()):
        return False
    row.used = True  # 一次性：用过即焚
    await db.commit()
    return True


# ============================================================
# 短信验证码（手机号登录/注册/绑定的凭证）
# ============================================================

async def issue_sms_code(db: AsyncSession, phone: str, ip: str) -> tuple[bool, str, str | None]:
    """给手机号发短信验证码（mock 模式不真发，返回测试码）。

    三重防轰炸：
      1. 冷却期：同一手机号 60 秒内只能发一条
      2. 日上限：同一手机号每天最多 sms_daily_limit 条
      3. IP 上限：同一 IP 每小时最多 N 条（防脚本换手机号狂发）

    返回 (是否成功, 提示信息, mock模式下返回的测试码或 None)
    """
    phone = phone.strip()
    now = _utcnow()

    # 拉该手机号最近记录，在 Python 里按统一时间判断（跨数据库一致）
    rows = (
        await db.scalars(
            select(VerificationCode)
            .where(VerificationCode.kind == "sms", VerificationCode.target == phone)
            .order_by(VerificationCode.created_at.desc())
            .limit(30)
        )
    ).all()

    # 1) 冷却期：60 秒内发过 → 拒绝
    if rows and now - _as_utc(rows[0].created_at) < dt.timedelta(seconds=settings.sms_cooldown_seconds):
        return False, f"发送太频繁，请 {settings.sms_cooldown_seconds} 秒后再试", None

    # 2) 每日上限
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = sum(1 for r in rows if _as_utc(r.created_at) >= today_start)
    if today_count >= settings.sms_daily_limit:
        return False, "该手机号今日发送次数已达上限", None

    # 3) 同 IP 每小时上限（mock 模式放宽到 30，便于本地反复测试）
    ip_rows = (
        await db.scalars(
            select(VerificationCode)
            .where(VerificationCode.kind == "sms", VerificationCode.ip == ip)
            .order_by(VerificationCode.created_at.desc())
            .limit(50)
        )
    ).all()
    ip_hour_limit = 30 if settings.sms_provider == "mock" else 10
    ip_count = sum(1 for r in ip_rows if now - _as_utc(r.created_at) < dt.timedelta(hours=1))
    if ip_count >= ip_hour_limit:
        return False, "发送过于频繁，请稍后再试", None

    # 生成并保存验证码（答案只存哈希）
    code = "123456" if settings.sms_provider == "mock" else f"{random.randint(0, 999999):06d}"
    db.add(
        VerificationCode(
            kind="sms",
            target=phone,
            ip=ip,
            code_hash=_sha256(code),
            expires_at=now + dt.timedelta(minutes=settings.sms_code_ttl_minutes),
        )
    )
    await db.commit()

    if settings.sms_provider == "mock":
        # 测试模式：不真发短信，验证码固定 123456，随接口返回方便前端开发
        logger.info("[SMS-MOCK] 给 %s 发送验证码 %s（测试模式）", phone, code)
        return True, "验证码已发送（测试模式）", code
    # 真实短信通道在此接入（上线前实现：调阿里云/腾讯云短信 API）
    logger.info("[SMS] 给 %s 发送验证码（真实通道待接入）", phone)
    return True, "验证码已发送", None


async def verify_sms_code(db: AsyncSession, phone: str, code: str, consume: bool = True) -> bool:
    """校验手机号+验证码；正确后默认作废该手机号所有未用验证码（防重放）。"""
    if not code:
        return False
    row = await db.scalar(
        select(VerificationCode)
        .where(
            VerificationCode.kind == "sms",
            VerificationCode.target == phone.strip(),
            VerificationCode.used.is_(False),
        )
        .order_by(VerificationCode.created_at.desc())
        .limit(1)
    )
    if (
        row is None
        or _as_utc(row.expires_at) < _utcnow()
        or row.code_hash != _sha256(code.strip())
    ):
        return False
    if consume:
        # 该手机号所有未用验证码全部作废，防止一个码反复登录
        await db.execute(
            delete(VerificationCode).where(
                VerificationCode.kind == "sms", VerificationCode.target == phone.strip()
            )
        )
        await db.commit()
    return True


# ============================================================
# 登录失败锁定（防撞库：同账号多 IP 轮着试 / 同 IP 批量试）
# ============================================================

async def check_login_locked(db: AsyncSession, username: str, ip: str) -> str | None:
    """检查账号/IP 是否处于锁定状态。

    命中则返回给用户看的中文提示；未命中返回 None。
    """
    window = dt.timedelta(minutes=settings.login_fail_lock_minutes)
    since = _utcnow() - window

    # 该用户名最近失败记录（Python 层按时间过滤，跨数据库一致）
    user_rows = (
        await db.scalars(
            select(LoginFailure)
            .where(LoginFailure.username == username)
            .order_by(LoginFailure.created_at.desc())
            .limit(50)
        )
    ).all()
    user_fails = sum(1 for r in user_rows if _as_utc(r.created_at) >= since)
    if user_fails >= settings.login_fail_lock_count:
        return f"输错次数过多，账号已临时锁定，请 {settings.login_fail_lock_minutes} 分钟后再试"

    # 该 IP 最近失败记录
    ip_rows = (
        await db.scalars(
            select(LoginFailure)
            .where(LoginFailure.ip == ip)
            .order_by(LoginFailure.created_at.desc())
            .limit(50)
        )
    ).all()
    ip_fails = sum(1 for r in ip_rows if _as_utc(r.created_at) >= since)
    if ip_fails >= settings.login_ip_fail_limit:
        return "尝试次数过多，请稍后再试"

    return None


async def record_login_failure(db: AsyncSession, username: str, ip: str) -> None:
    """记录一次登录失败。"""
    db.add(LoginFailure(username=username, ip=ip))
    await db.commit()


async def clear_login_failures(db: AsyncSession, username: str) -> None:
    """登录成功时清零该账号的失败记录（给真实用户"改过自新"的机会）。"""
    await db.execute(delete(LoginFailure).where(LoginFailure.username == username))
    await db.commit()
