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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import DbSession, login_rate_limiter, sha256_hex
from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    validate_password_strength,
    verify_password,
)
from app.models.organization import Organization
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import LogoutRequest, RefreshRequest, TokenPair
from app.schemas.user import (
    BindPhoneRequest,
    CaptchaOut,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterPolicyOut,
    ResetPasswordRequest,
    SimpleMessageOut,
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


# ============ 注册策略（公开、只读）============

@router.get("/register-policy", response_model=RegisterPolicyOut)
async def get_register_policy():
    """告诉前端"注册时邀请码是选填还是必填"，用于把注册页的提示文字说清楚。

    这是公开接口（无需登录）：注册页在用户登录之前就要展示，登录不了就取不到。
    返回内容只有一个布尔值，不含任何组织名、邀请码等敏感信息。
    """
    return RegisterPolicyOut(invite_required=settings.registration_requires_invite)


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

    # 组织归属：决定这个账号以后能看到哪些知识库
    org_id = await _resolve_org_for_register(db, payload.invite_code)

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        nickname=payload.nickname or payload.username,
        role="user",
        register_ip=ip,
        org_id=org_id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _resolve_org_for_register(db: DbSession, invite_code: str) -> int | None:
    """按邀请码决定新账号加入哪个组织。

    规则：
      - 填了邀请码 → 校验后加入该组织（码错/组织停用则直接拒绝注册）；
      - 没填      → 看配置 registration_requires_invite：
                      False（私有部署）→ 加入默认组织，同事自助注册即可；
                      True（对外 SaaS）→ 拒绝，必须凭码注册。

    为什么要给这个开关：
        一套系统"只服务一家公司"和"服务多家客户"的安全要求完全不同。
        前者不填码就进默认组织是方便；后者就是漏洞——陌生人注册进来就能看到客户资料。
    """
    code = (invite_code or "").strip()

    if code:
        org = await db.scalar(select(Organization).where(Organization.invite_code == code))
        if org is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "邀请码无效，请向管理员确认")
        if not org.is_active:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "该组织已停用，无法加入")
        return org.id

    if settings.registration_requires_invite:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "本系统需要组织邀请码才能注册，请向管理员索取"
        )

    # 不强制邀请码：落到最早的（默认）组织，保证注册后至少有个正常的工作范围
    org = (await db.execute(select(Organization).order_by(Organization.id))).scalars().first()
    return org.id if org else None


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
        # 组织归属走与"用户名注册"完全相同的规则，避免两条自助入口的反作弊策略不一致
        org_id = await _resolve_org_for_register(db, "")
        random_pw = secrets.token_urlsafe(24)
        user = User(
            username="u" + phone,
            password_hash=hash_password(random_pw),
            nickname="用户" + phone[-4:],
            role="user",
            phone=phone,
            org_id=org_id,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        logger.info("手机号 %s 首次验证码登录，已自动创建账号", phone)
    elif not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用，请联系管理员")

    return await _issue_tokens(db, user)


# ============ 忘记密码 / 重置密码 ============
#
# 为什么需要这一组接口：
#   账号密码登录是老用户唯一的入口。一旦忘记密码，原来只能人工找管理员改库，
#   既有安全风险（管理员手上有别人的密码），也把客服绑死。这里补上自助恢复通道。
#
# 整体思路（两步）：
#   第一步 /password/forgot —— 证明"我确实拿着这个手机号"：先答图形验证码（挡脚本），
#                              再往已绑定手机号发一条重置短信验证码。
#   第二步 /password/reset  —— 用收到的短信验证码换一个新密码。
#
# 两个安全要点：
#   ① **不暴露手机号是否已注册**（防账号枚举）：账号不存在时也回同一句话。
#   ② **重置成功要把所有设备踢下线**：密码重置最常见的场景就是"账号被盗"，
#      如果旧设备的登录状态还能继续用，等于没改。


@router.post("/password/forgot", response_model=SmsSendOut)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: DbSession,
    _: None = Depends(login_rate_limiter),
):
    """忘记密码第一步：往已绑定的手机号发一条重置验证码。"""
    ip = _client_ip(request)
    phone = payload.phone.strip()

    # 图形验证码：挡住"脚本批量刷短信"（短信是要花钱的，还能被当成骚扰工具）
    if not await auth_guard.consume_captcha(db, payload.captcha_id, payload.captcha_code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图形验证码错误或已过期，请刷新后重试")

    # 统一对外话术：无论账号存不存在，说的都是同一句
    generic_msg = "如果该手机号已绑定账号，重置验证码已发送，请留意短信"

    user = await db.scalar(select(User).where(User.phone == phone))
    if user is None or not user.is_active:
        # 账号不存在 / 已被禁用：不发短信，但也**不能**告诉对方"查无此人"，
        # 否则这个接口就变成了"手机号是否注册过"的查询器。
        logger.info("忘记密码：手机号 %s 无可用账号，已按统一话术回复（防账号枚举）", phone)
        return SmsSendOut(message=generic_msg, dev_code=None, retry_after=settings.sms_cooldown_seconds)

    ok, msg, dev_code = await auth_guard.issue_sms_code(db, phone, ip)
    if not ok:
        # 冷却/超限：这类提示与"账号是否存在"无关，可以如实告诉用户
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg)
    return SmsSendOut(message=generic_msg, dev_code=dev_code, retry_after=settings.sms_cooldown_seconds)


@router.post("/password/reset", response_model=SimpleMessageOut)
async def reset_password(
    payload: ResetPasswordRequest,
    db: DbSession,
    _: None = Depends(login_rate_limiter),
):
    """忘记密码第二步：凭短信验证码设置新密码，并踢掉所有旧登录状态。"""
    phone = payload.phone.strip()

    # 先"验证但先不作废"（consume=False）。
    # 为什么要多这一步：如果直接作废，用户只是密码填得不合规范（比如太简单），
    # 这一条短信验证码就白费了，还得重新获取一条。所以顺序是
    # 验码 → 查账号 → 校验新密码强度 → 最后才真正把码用掉。
    if not await auth_guard.verify_sms_code(db, phone, payload.code, consume=False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码错误或已过期，请重新获取")

    user = await db.scalar(select(User).where(User.phone == phone))
    if user is None or not user.is_active:
        # 到这一步还查不到账号，说明验证码和手机号对不上。统一提示，不透露账号情况。
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码错误或已过期，请重新获取")

    # 密码强度必须在这里校验：规则里有"不能包含用户名"，只有查到账号后才拿得到用户名
    ok, reason = validate_password_strength(payload.new_password, user.username)
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, reason)

    # 到这一步才真正消费验证码（一次性，防重放）
    await auth_guard.verify_sms_code(db, phone, payload.code, consume=True)

    user.password_hash = hash_password(payload.new_password)
    # 关键一步：把该账号名下所有还没过期的刷新令牌全部作废。
    # 不这么做的话，"账号被盗 → 改密码"之后，盗号者的登录状态仍然有效，等于白改。
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
        .values(revoked=True)
    )
    await db.commit()
    logger.info("用户 %s 已通过短信验证码重置密码，旧登录状态已全部失效", user.username)
    return SimpleMessageOut(message="密码已重置，请用新密码登录")


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
