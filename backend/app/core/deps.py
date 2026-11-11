"""FastAPI 依赖：鉴权、角色校验、登录限流。"""

import hashlib
import time
from collections import defaultdict, deque
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_cookies import get_access_token
from app.core.security import TokenError, decode_token
from app.db.session import get_db
from app.models.user import User

# 令牌来源已移到 app/core/auth_cookies.py 统一处理
# （优先 HttpOnly Cookie，回退 Authorization 头）

# 简单的登录防爆破：记录每个 IP 最近 1 分钟内的登录尝试次数（内存实现）
_login_attempts: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_MAX_PER_MINUTE = 10


def _check_login_rate(ip: str) -> None:
    """1 分钟内登录尝试超过 10 次就临时拒绝，防止密码被暴力猜。"""
    now = time.monotonic()
    bucket = _login_attempts[ip]
    while bucket and now - bucket[0] > 60:  # 清掉 1 分钟前的记录
        bucket.popleft()
    if len(bucket) >= _LOGIN_MAX_PER_MINUTE:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "尝试次数过多，请 1 分钟后再试")
    bucket.append(now)


def sha256_hex(text: str) -> str:
    """令牌只存哈希（单向），防数据库泄露。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """解析访问令牌 → 查出对应用户；无效则 401。

    令牌来源（按优先级）：
        ① HttpOnly Cookie —— 浏览器端用这个，JS 读不到，XSS 偷不走；
        ② Authorization: Bearer <token> —— 命令行/脚本/第三方调用方用这个。

    两种都支持是刻意的：安全上浏览器走 Cookie，兼容上保留 Bearer，
    两者校验逻辑完全一样（同一套 decode_token）。
    """
    token = get_access_token(request)
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "未登录或登录已过期", headers={"WWW-Authenticate": "Bearer"}
        )
    try:
        payload = decode_token(token, expected_type="access")
    except TokenError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, str(exc), headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    user = await db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已被禁用")
    return user


async def require_admin(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    """管理员专用接口的依赖：非 admin 一律 403。

    多租户语境下这代表"组织管理员"——他能管的是**本组织**的知识库与成员，
    具体的组织范围校验在各自的接口里通过 core.tenancy 完成。
    """
    if current_user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return current_user


def is_superadmin(user: User) -> bool:
    """是否为平台超管（唯一的权威判定，全项目都用这一个）。

    为什么要单独抽出来：
        这段判断原本在 11 个地方各写一遍，还有两个模块各有一个
        叫 _is_superadmin 的函数。将来要调整判定规则
        （比如"账号被禁用就不算超管"），改 11 处漏一处就是权限漏洞。

    为什么用 getattr 而不是直接 user.is_superadmin：
        测试里常常用简化的假对象（没有 is_superadmin 字段），
        直接访问会 AttributeError。这里统一兜底为 False（即"不是超管"），
        是更安全的方向 —— 找不到信息时按"没有特权"处理。
    """
    return bool(getattr(user, "is_superadmin", False))


async def require_superadmin(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    """平台超管专用接口的依赖（创建/管理组织）。

    为什么单独区分这一层：
        组织管理员只能在"自己组织"里管东西；而"新建客户组织"是跨组织的动作，
        属于平台运营方（你自己）的权限。两者混在一起，任何一个客户的
        管理员都能给自己开一个"新组织"，计费与隔离就都失控了。
    """
    if not is_superadmin(current_user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要平台管理员权限")
    return current_user


# 类型别名，让路由签名更简洁
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
SuperAdminUser = Annotated[User, Depends(require_superadmin)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def login_rate_limiter(request: Request) -> None:
    """登录接口专用依赖：取客户端 IP 做限流。"""
    _check_login_rate(request.client.host if request.client else "unknown")
