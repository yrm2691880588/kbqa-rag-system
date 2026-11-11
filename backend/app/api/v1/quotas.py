"""额度管理接口：查用量、改额度、整体停服。

谁能用哪个：
    GET  /quotas/me           —— 登录用户都能看**自己组织**的用量（不跨组织）
    GET  /quotas/{org_id}     —— 平台超管看任意组织；组织管理员只能看自己那个（否则会变成
                                  "挨个数字试出别家公司用了多少"的信息泄露口）
    PUT  /quotas/{org_id}     —— 只有平台超管能改。为什么不让组织管理员自己改：
                                  额度就是合同，客户自己能把上限调高的话，"上限"就没有意义了。

所有"按 org_id 取数"的地方都先过一遍权限校验，不裸查数据库。
"""

import logging

from fastapi import APIRouter, HTTPException, status

from app.core.deps import is_superadmin, CurrentUser, DbSession, SuperAdminUser
from app.models.organization import Organization
from app.schemas.quota import QuotaOut, QuotaUpdate, UsageSummaryOut
from app.services import quota as quota_service

logger = logging.getLogger("kbqa.api.quotas")

router = APIRouter(prefix="/quotas", tags=["额度与用量"])


def _is_superadmin(user) -> bool:
    """兼容旧名；实现统一走 core.deps.is_superadmin。"""
    return is_superadmin(user)


async def _assert_can_view(user, org_id: int) -> None:
    """校验"这个账号能不能看这个组织的用量"。

    规则：平台超管随便看；其余人只能看自己组织。
    越权返回 **404** 而不是 403 —— 403 等于告诉对方"这个组织存在，只是你没权限"，
    可以被用来逐个数字枚举出"平台上一共有哪些客户"。
    """
    if _is_superadmin(user):
        return
    if getattr(user, "org_id", None) != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")


@router.get("/me", response_model=UsageSummaryOut)
async def get_my_usage(current_user: CurrentUser, db: DbSession):
    """看自己组织的用量与额度（问答页/管理页都要用）。"""
    org_id = getattr(current_user, "org_id", None)
    if org_id is None:
        # 平台超管不隶属任何组织，没有"自己的"用量可查
        raise HTTPException(status.HTTP_404_NOT_FOUND, "当前账号不属于任何组织")
    return await quota_service.get_summary(db, org_id)


@router.get("/{org_id}", response_model=UsageSummaryOut)
async def get_org_usage(org_id: int, current_user: CurrentUser, db: DbSession):
    """查看指定组织的用量（平台超管用；组织管理员看自己那个）。"""
    await _assert_can_view(current_user, org_id)
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")
    return await quota_service.get_summary(db, org_id)


@router.get("/{org_id}/config", response_model=QuotaOut)
async def get_org_quota(org_id: int, current_user: CurrentUser, db: DbSession):
    """查某组织的额度配置（没配过就返回一份"全部不限"的默认值，不落库）。"""
    await _assert_can_view(current_user, org_id)
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")
    q = await quota_service.get_quota(db, org_id)
    if q is None:
        return QuotaOut(org_id=org_id, note="尚未设置：当前不限制用量")
    return q


@router.put("/{org_id}", response_model=QuotaOut)
async def update_org_quota(
    org_id: int, payload: QuotaUpdate, current_user: SuperAdminUser, db: DbSession
):
    """设置额度（仅平台超管）。

    只改"请求体里真正出现的字段"：
        没写的字段保持原样；显式写 null 表示改成"不限"。
    这样运营改备注时不会把额度顺手清空。
    """
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")

    q = await quota_service.get_or_create_quota(db, org_id)
    data = payload.model_dump(exclude_unset=True)

    for field_name, value in data.items():
        if field_name == "is_service_enabled":
            if value is None:
                continue
            q.is_service_enabled = bool(value)
        elif field_name == "note":
            q.note = value or ""
        else:
            setattr(q, field_name, value)

    await db.commit()
    await db.refresh(q)
    logger.info(
        "平台超管 %s 调整了组织 %s 的额度：%s",
        current_user.username, org_id, data,
    )
    return q
