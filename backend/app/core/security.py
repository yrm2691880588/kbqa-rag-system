"""安全模块：密码哈希(argon2) + JWT 令牌签发/校验 + 密码强度校验。"""

import datetime as dt
import re
import secrets
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from app.core.config import settings

# argon2 密码哈希器（抗暴力破解，比 md5/sha 安全得多）
_ph = PasswordHasher()

ALGORITHM = "HS256"

# 常见弱密码黑名单（来自历年泄露数据的高频密码，注册/改密直接拒绝）
_WEAK_PASSWORDS = {
    "12345678", "123456789", "1234567890", "password", "password1", "qwerty123",
    "qwertyui", "11111111", "00000000", "abc12345", "abcd1234", "123123123",
    "admin123", "administrator", "a12345678", "passw0rd", "iloveyou", "sunshine",
    "qazwsxedc", "1qaz2wsx",
}


def validate_password_strength(password: str, username: str = "") -> tuple[bool, str]:
    """校验密码强度，返回 (是否合格, 不合格原因)。

    规则（注册与修改密码共用，保证全站一致）：
      1. 长度 8-64 位
      2. 必须同时包含字母和数字（杜绝纯 1、纯字母）
      3. 不能是常见弱密码
      4. 不能包含用户名（防止 用户名+123 这种偷懒组合）
    """
    if not password or len(password) < 8:
        return False, "密码至少需要 8 位"
    if len(password) > 64:
        return False, "密码最长 64 位"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return False, "密码必须同时包含字母和数字"
    if password.lower() in _WEAK_PASSWORDS:
        return False, "这个密码太常见了，容易被猜中，请换一个"
    if username and len(username) >= 4 and username.lower() in password.lower():
        return False, "密码不能包含用户名，请换一个"

    return True, ""


def hash_password(plain: str) -> str:
    """把明文密码变成不可逆的哈希串，数据库只存哈希。"""
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验用户输入的密码是否与库里哈希匹配。"""
    try:
        return _ph.verify(hashed, plain)
    except Argon2Error:
        return False


def _create_token(user_id: int, role: str, token_type: str, expires_delta: dt.timedelta) -> str:
    """签发 JWT：包含用户 id、角色、令牌类型与过期时间。

    带 jti 随机编号：即使同一用户在同一秒内签两次，令牌内容也绝不重复
    （refresh 令牌在库中按哈希唯一存储，重复会造成写入冲突）。
    """
    now = dt.datetime.now(dt.timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),          # 主题：用户 id
        "role": role,                 # 角色：admin / user
        "type": token_type,           # 令牌类型：access / refresh
        "jti": secrets.token_hex(16),  # 唯一编号：保证令牌永不重复
        "iat": now,                   # 签发时间
        "exp": now + expires_delta,   # 过期时间
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def create_access_token(user_id: int, role: str) -> str:
    """短时访问令牌（默认 2 小时），每次请求鉴权用。"""
    delta = dt.timedelta(minutes=settings.access_token_expire_minutes)
    return _create_token(user_id, role, "access", delta)


def create_refresh_token(user_id: int, role: str) -> str:
    """长时刷新令牌（默认 7 天），仅用于换取新的访问令牌，可注销。"""
    delta = dt.timedelta(days=settings.refresh_token_expire_days)
    return _create_token(user_id, role, "refresh", delta)


class TokenError(Exception):
    """自定义令牌异常，便于上层统一转成 401。"""


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    """校验并解析令牌；类型不符/过期/签名错误都抛 TokenError。"""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:  # 过期、签名错误等
        raise TokenError("令牌无效或已过期") from exc
    if payload.get("type") != expected_type:
        raise TokenError("令牌类型错误")
    return payload
