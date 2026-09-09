"""认证接口：图形验证码 / 注册 / 账号密码登录 / 短信验证码登录 / 刷新令牌 / 登出。

安全设计（2026-09 加固）：
- 注册必须答对图形验证码（防机器人批量刷号）+ 同 IP 每小时限 3 个账号
- 登录连续失败锁定：同账号 15 分钟错 5 次 / 同 IP 15 分钟错 20 次 → 临时锁定
- 发短信验证码必须先答对图形验证码（防短信轰炸）+ 手机号 60 秒冷却
- 手机号验证码登录：号码未注册过则自动创建账号（一步到位）
"""

import datetime as dt
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import DbSession, login_rate_limiter, sha256_hex
from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import LogoutRequest, RefreshRequest, TokenPair
from app.schemas.user import (
    BindPhoneRequest,
    CaptchaOut,
    LoginRequest,
    SmsLoginRequest,
    SmsSendOut,
    SmsSendRequest,
    UserOut,
    UserRegister,
)
from app.services import auth_guard

logger = logging.getLogger("kbqa.auth")

router = APIRouter(prefix="/auth", tags=["认证"])


def _client_ip(request: Request) -> str:
    """取客户端 IP（取不到给 unknown）。"""
    return request.client.host if request.client else "unknown"


def _as_utc(value: dt.datetime) -> dt.datetime:
    """数据库取回的时间可能没带时区（SQLite 就是如此），统一补成 UTC 再比较。"""
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


async def _issue_tokens(db: AsyncSession, user: User) -> TokenPair:
    """登录成功后的公共收尾：更新登录时间、登记刷新令牌、签发双令牌。"""
    user.last_login_at = dt.datetime.now(dt.timezone.utc)
    refresh_token = create_refresh_token(user.id, user.role)
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=sha256_hex(refresh_token),
            expires_at=dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(days=settings.refresh_token_expire_days),
        )
    )
    await db.commit()
    await db.refresh(user)
    return TokenPair(
        access_token=create_access_token(user.id, user.role),
        refresh_token=refresh_token,
        user=UserOut.model_validate(user),
    )


# ============ 图形验证码（人机校验）============

@router.get("/captcha", response_model=CaptchaOut)
async def get_captcha(db: DbSession):
    """获取一张图形验证码：注册 / 发短信前需要答对它。"""
    captcha_id, image = await auth_guard.issue_captcha(db)
    return CaptchaOut(
        captcha_id=captcha_id,
        image_base64=image,
        expires_in=settings.captcha_ttl_minutes * 60,
    )


# ============ 注册（防机器人）============

@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: UserRegister, request: Request, db: DbSession):
    """用户自助注册：需答对图形验证码；同 IP 每小时最多注册 3 个账号。"""
    ip = _client_ip(request)

    # 第一道闸：同 IP 注册频率（防脚本批量刷号）
    hour_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    recent_regs = (
        await db.scalars(
            select(User)
            .where(User.register_ip == ip)
            .order_by(User.created_at.desc())
            .limit(10)
        )
    ).all()
    reg_count = sum(1 for u in recent_regs if _as_utc(u.created_at) >= hour_ago)
    if reg_count >= settings.register_max_per_ip_hour:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "注册过于频繁，请稍后再试")

    # 第二道闸：图形验证码（机器人答不对）
    if not await auth_guard.consume_captcha(db, payload.captcha_id, payload.captcha_code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图形验证码错误或已过期，请刷新后重试")

    exists = await db.scalar(select(User).where(User.username == payload.username))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已被注册，请换一个")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        nickname=payload.nickname or payload.username,
        role="user",
        register_ip=ip,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ============ 账号密码登录（防撞库）============

@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, request: Request, db: DbSession, _: None = Depends(login_rate_limiter)):
    """账号密码登录：失败锁定 + 成功签发双令牌。"""
    ip = _client_ip(request)
    username = payload.username.strip()

    # 锁定检查：该账号/IP 近期失败过多则直接拒绝
    locked_msg = await auth_guard.check_login_locked(db, username, ip)
    if locked_msg:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, locked_msg)

    user = await db.scalar(select(User).where(User.username == username))
    # 统一提示，不暴露"用户名是否存在"（防用户名枚举）
    if user is None or not verify_password(payload.password, user.password_hash):
        await auth_guard.record_login_failure(db, username, ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用，请联系管理员")

    # 登录成功：清零该账号的失败记录
    await auth_guard.clear_login_failures(db, username)
    return await _issue_tokens(db, user)


# ============ 短信验证码：发送 ============

@router.post("/sms/send", response_model=SmsSendOut)
async def sms_send(payload: SmsSendRequest, request: Request, db: DbSession, _: None = Depends(login_rate_limiter)):
    """给手机号发短信验证码：先验图形验证码（防轰炸），再按冷却/日限发放。"""
    ip = _client_ip(request)

    if not await auth_guard.consume_captcha(db, payload.captcha_id, payload.captcha_code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图形验证码错误或已过期，请刷新后重试")

    ok, msg, dev_code = await auth_guard.issue_sms_code(db, payload.phone, ip)
    if not ok:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg)
    return SmsSendOut(message=msg, dev_code=dev_code, retry_after=settings.sms_cooldown_seconds)


# ============ 短信验证码：登录 / 自动注册 ============

@router.post("/sms/login", response_model=TokenPair)
async def sms_login(payload: SmsLoginRequest, db: DbSession, _: None = Depends(login_rate_limiter)):
    """手机号 + 短信验证码 登录。

    该手机号已绑定账号 → 直接登录该账号；
    从未注册过 → 自动创建账号（手机号即是登录凭证，无需先注册）。
    """
    phone = payload.phone.strip()

    if not await auth_guard.verify_sms_code(db, phone, payload.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码错误或已过期，请重新获取")

    user = await db.scalar(select(User).where(User.phone == phone))

    if user is None:
        # 自动注册：用户名 = u + 手机号（唯一），密码为随机值（此账号只用手机号登录）
        random_pw = secrets.token_urlsafe(24)
        user = User(
            username="u" + phone,
            password_hash=hash_password(random_pw),
            nickname="用户" + phone[-4:],
            role="user",
            phone=phone,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        logger.info("手机号 %s 首次验证码登录，已自动创建账号", phone)
    elif not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用，请联系管理员")

    return await _issue_tokens(db, user)


# ============ 刷新令牌 / 登出 ============

async def _rotate_refresh(db: AsyncSession, refresh_token: str) -> tuple[str, str]:
    """校验刷新令牌 → 作废旧令牌 → 发一对新令牌（令牌轮换，更安全）。"""
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期，请重新登录") from exc

    user_id = int(payload["sub"])
    token_hash = sha256_hex(refresh_token)
    record = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))

    if record is None or record.revoked or record.user_id != user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效，请重新登录")
    if record.expires_at < dt.datetime.now(dt.timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期，请重新登录")

    # 作废旧令牌（防重放），签发新的一对
    record.revoked = True
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已被禁用")

    new_refresh = create_refresh_token(user.id, user.role)
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=sha256_hex(new_refresh),
            expires_at=dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(days=settings.refresh_token_expire_days),
        )
    )
    await db.commit()
    return create_access_token(user.id, user.role), new_refresh


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: DbSession):
    """访问令牌过期后，用刷新令牌静默换新，用户无需重新输密码。"""
    access_token, new_refresh = await _rotate_refresh(db, payload.refresh_token)
    user_id = int(decode_token(access_token, expected_type="access")["sub"])
    user = await db.get(User, user_id)
    return TokenPair(
        access_token=access_token,
        refresh_token=new_refresh,
        user=UserOut.model_validate(user),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: LogoutRequest, db: DbSession):
    """退出登录：把该刷新令牌作废，此后无法再换新令牌。"""
    record = await db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == sha256_hex(payload.refresh_token))
    )
    if record is not None:
        record.revoked = True
        await db.commit()
