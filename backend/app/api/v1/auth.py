"""认证接口：注册 / 登录 / 刷新令牌 / 登出。"""

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
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
from app.schemas.user import LoginRequest, UserOut, UserRegister

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: UserRegister, db: DbSession):
    """用户自助注册：默认普通用户角色，用户名全局唯一。"""
    exists = await db.scalar(select(User).where(User.username == payload.username))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已被注册，请换一个")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        nickname=payload.nickname or payload.username,
        role="user",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: DbSession, _: None = Depends(login_rate_limiter)):
    """账号密码登录：成功签发 双令牌（access+refresh）并返回用户信息。"""
    user = await db.scalar(select(User).where(User.username == payload.username))
    # 统一提示，不暴露"用户名是否存在"
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用，请联系管理员")

    # 记录登录时间 + 签发刷新令牌（库中只存哈希，便于登出作废）
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
