"""API Key 鉴权：给"程序对程序"的调用提供一条独立的鉴权通道。

和登录令牌（JWT）的区别，决定了它必须单独实现：
    · JWT 是**人**的凭证，2 小时过期，必须配合刷新令牌；
    · API Key 是**程序**的凭证，长期有效，可随时吊销，不带"人"的身份。
    把两者混在一套逻辑里，结果就是要么 Key 频繁失效（对接方天天报故障），
    要么把 JWT 的有效期拉长到几个月（人这边的安全性就崩了）。

三条安全约定：
    ① 只比对 sha256 哈希，数据库里没有明文，服务器也还原不出来；
    ② Key 只能访问**它所属组织**的数据 —— 企业 A 的 Key 拿不到企业 B 的知识库；
    ③ 每次调用都会更新"最后使用时间"与调用次数，便于发现异常（例如某个 Key 半夜被刷）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.api_key import ApiKey

logger = logging.getLogger("kbqa.api_key")

# 明文 Key 的固定前缀。作用是"一眼能认出来"：
#   在日志里看到一个 kbqa_ 开头的串，就知道那是本系统的 Key，而不是别的东西。
KEY_PREFIX = "kbqa_"


def hash_key(raw: str) -> str:
    """Key 只存哈希，和密码一个道理。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def looks_like_api_key(token: str) -> bool:
    """判断一个 Bearer 令牌是不是 API Key（用来和 JWT 区分）。

    为什么需要区分：两者都放在 Authorization: Bearer 头里，
    但一个是 JWT（三段点号分隔）、一个是 kbqa_ 开头的随机串。
    靠前缀区分，就不用让对接方额外记一个请求头名字。
    """
    return bool(token) and token.startswith(KEY_PREFIX)


@dataclass
class ApiClient:
    """一次成功的 API Key 调用所代表的身份。"""

    key_id: int
    org_id: int
    name: str
    raw_prefix: str


def _extract_raw_key(request: Request) -> str | None:
    """从请求里取出 API Key 明文。

    支持两种写法（对接方习惯不同，两种都认）：
        Authorization: Bearer kbqa_xxxxx     ← 主流写法
        X-API-Key: kbqa_xxxxx                ← 部分网关/简易客户端用这个
    """
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if looks_like_api_key(token):
            return token
    header_key = (request.headers.get("x-api-key") or "").strip()
    if header_key:
        return header_key
    return None


async def require_api_key(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ApiClient:
    """对外接口的鉴权依赖：校验 API Key，返回它代表的组织身份。

    失败一律返回 401，并且提示语统一 —— 不区分"Key 不存在""Key 已吊销"
    "Key 已过期"，避免给试探者任何反馈信号。
    """
    raw = _extract_raw_key(request)
    if not raw:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "缺少 API Key。请在请求头带上 Authorization: Bearer <你的Key>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    row = await db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
    invalid = HTTPException(status.HTTP_401_UNAUTHORIZED, "API Key 无效、已吊销或已过期")

    if row is None:
        logger.warning("收到无法识别的 API Key（前缀 %s）", raw[:12])
        raise invalid
    if row.revoked:
        raise invalid
    if row.expires_at is not None:
        # 数据库取回的时间可能不带时区（SQLite 就是如此），统一补成 UTC 再比较
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=dt.timezone.utc)
        if exp < dt.datetime.now(dt.timezone.utc):
            raise invalid

    # 记账：最后使用时间 + 调用次数。
    # 这一步失败不能影响正常调用（只是统计信息），所以吞掉异常。
    try:
        row.last_used_at = dt.datetime.now(dt.timezone.utc)
        row.use_count = (row.use_count or 0) + 1
        await db.commit()
    except Exception:
        logger.warning("更新 API Key 使用记录失败（不影响本次调用）", exc_info=True)
        await db.rollback()

    return ApiClient(key_id=row.id, org_id=row.org_id, name=row.name or "", raw_prefix=row.key_prefix)


# 类型别名：路由签名里写 ApiClientDep 即可
ApiClientDep = Annotated[ApiClient, Depends(require_api_key)]
