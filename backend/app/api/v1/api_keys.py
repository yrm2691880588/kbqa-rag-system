"""API Key 管理接口：创建 / 查看 / 吊销。

使用流程（给客户看的）：
    1. 管理员在后台建一个 Key，起个名字（例如"客服系统正式环境"）；
    2. **立刻复制**（只显示一次），填到自家系统的配置里；
    3. 调用 POST /api/v1/open/chat 就能问答，不用再登录。

权限：
    创建与吊销都需要管理员（组织管理员管自己组织；平台超管可指定组织）。
    查看列表同样要管理员 —— 虽然列表不含明文，但"这家公司开了几个 Key、
    什么时候被用过"也属于运营信息。
"""

import logging
import secrets

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.core.api_key_auth import KEY_PREFIX, hash_key
from app.core.deps import AdminUser, DbSession
from app.models.api_key import ApiKey
from app.models.organization import Organization
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreatedOut, ApiKeyOut

logger = logging.getLogger("kbqa.api.api_keys")

router = APIRouter(prefix="/api-keys", tags=["对外 API 凭证"])


def _resolve_org(current_user, requested_org_id: int | None) -> int:
    """决定这个 Key 归属哪个组织。

    规则：
        平台超管 → 必须显式指定 org_id（他自己的账号不属于任何组织）；
        其他管理员 → 只能是自己的组织，请求里填了别的也忽略（不给越权留口子）。
    """
    if getattr(current_user, "is_superadmin", False):
        if requested_org_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "平台超管请指定 org_id（给哪个组织开 Key）")
        return requested_org_id
    if current_user.org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前账号不属于任何组织，无法创建 API Key")
    return current_user.org_id


@router.post("", response_model=ApiKeyCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_api_key(payload: ApiKeyCreate, current_user: AdminUser, db: DbSession):
    """创建一个 API Key。返回的明文 key **只出现这一次**。"""
    import datetime as dt

    org_id = _resolve_org(current_user, payload.org_id)
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")
    if not org.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该组织已停用，无法创建 API Key")

    # 随机部分用密码学安全随机数：可猜的 Key 等于没有鉴权
    secret_part = secrets.token_urlsafe(32)
    raw = f"{KEY_PREFIX}{secret_part}"

    row = ApiKey(
        org_id=org_id,
        name=payload.name or "",
        key_hash=hash_key(raw),
        # 只留前 12 位用于展示/排查：这个长度足够区分是哪一个 Key，
        # 又不足以反过来拼出完整 Key。
        key_prefix=raw[:12],
        created_by=current_user.id,
        expires_at=(
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=payload.expires_days)
            if payload.expires_days else None
        ),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info("管理员 %s 为组织 %s 创建了 API Key「%s」(id=%d)",
                current_user.username, org_id, row.name, row.id)

    return ApiKeyCreatedOut(
        id=row.id, org_id=row.org_id, name=row.name, key_prefix=row.key_prefix,
        revoked=row.revoked, created_by=row.created_by, expires_at=row.expires_at,
        last_used_at=row.last_used_at, use_count=row.use_count, created_at=row.created_at,
        key=raw,
    )


@router.get("", response_model=list[ApiKeyOut])
async def list_api_keys(
    current_user: AdminUser,
    db: DbSession,
    org_id: int | None = Query(None, description="平台超管可指定；组织管理员忽略"),
):
    """列出 API Key（不含明文）。"""
    scope = _resolve_org(current_user, org_id) if getattr(current_user, "is_superadmin", False) else current_user.org_id
    if scope is None:
        return []
    rows = (
        await db.execute(
            select(ApiKey).where(ApiKey.org_id == scope).order_by(ApiKey.id.desc())
        )
    ).scalars().all()
    return rows


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(key_id: int, current_user: AdminUser, db: DbSession):
    """吊销一个 API Key（立即失效）。

    实现上是"打标记"而不是删行：这条 Key 是哪个员工在什么时候建的、
    总共被调用了多少次，都是排查问题时要用的信息，删了就查不到了。
    """
    row = await db.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API Key 不存在")

    # 越权一律 404：不能让别人知道"这个 Key 存在过"
    if not getattr(current_user, "is_superadmin", False):
        if current_user.org_id is None or row.org_id != current_user.org_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "API Key 不存在")

    row.revoked = True
    db.add(row)
    await db.commit()
    logger.info("管理员 %s 吊销了 API Key id=%d（组织 %s）",
                current_user.username, key_id, row.org_id)
