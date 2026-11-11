"""组织（租户）管理接口。

权限划分：
- `/organizations/me`              当前用户查看自己所属组织（任何登录用户）
- 其余接口                          平台超管（is_superadmin）专用
  + 例外：`/{org_id}/members` 允许"本组织管理员"查看自己组织的成员

为什么"新建组织"要限制成超管：
    新建组织 = 新开一个客户。如果任何组织管理员都能建，那隔离就没有意义了
    （自己给自己开一个新组织，等于绕过所有限制）。这是平台的经营动作，属于运营方权限。
"""

import logging
import secrets

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.core.deps import is_superadmin, CurrentUser, DbSession, SuperAdminUser
from app.models.knowledge_base import KnowledgeBase
from app.models.organization import Organization
from app.models.user import User
from app.schemas.organization import (
    InviteCodeOut,
    OrgCreate,
    OrgCreatedOut,
    OrgOut,
    OrgUpdate,
)
from app.schemas.user import UserOut

logger = logging.getLogger("kbqa.api.org")

router = APIRouter(prefix="/organizations", tags=["组织管理"])


async def _counts(db, org_id: int) -> tuple[int, int]:
    """统计某组织的成员数与知识库数（列表展示用）。"""
    members = await db.scalar(
        select(func.count()).select_from(User).where(User.org_id == org_id)
    )
    kbs = await db.scalar(
        select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.org_id == org_id)
    )
    return int(members or 0), int(kbs or 0)


async def _counts_bulk(db) -> tuple[dict[int, int], dict[int, int]]:
    """一次性查出所有组织的成员数与知识库数。

    为什么需要这个函数：
        列表页是逐行渲染的，如果每行都调一次 _counts()，
        有 20 家客户时这一页就要打 40 次查询（典型的 N+1）。
        这里用两条 GROUP BY 把全部组织的计数一次性拿回来后按 org_id 索引。

        早期版本在注释里写着"用两条聚合查询一次拿全，避免 N+1"，
        但实际循环体里仍是 `for o in orgs: await _counts(db, o.id)` ——
        注释与实现相反，N+1 一直在。
    """
    member_rows = (
        await db.execute(
            select(User.org_id, func.count()).where(User.org_id.isnot(None)).group_by(User.org_id)
        )
    ).all()
    kb_rows = (
        await db.execute(
            select(KnowledgeBase.org_id, func.count())
            .where(KnowledgeBase.org_id.isnot(None))
            .group_by(KnowledgeBase.org_id)
        )
    ).all()
    return (
        {int(org_id): int(n or 0) for org_id, n in member_rows},
        {int(org_id): int(n or 0) for org_id, n in kb_rows},
    )


async def _usage_and_quota_maps(db) -> tuple[dict[int, int], dict[int, object]]:
    """一次性把"各组织本月用量"和"各组织额度"查出来。

    为什么要批量查、而不是每个组织查一次：
        组织列表是逐行渲染的，如果每行都单独查两次数据库，
        有 20 家客户时这一页就要打 40 次查询（N+1 问题），页面会明显变慢。
        这里用两条聚合查询把所有组织一次拿全。
    """
    from app.models.org_quota import OrgQuota
    from app.models.usage_daily import UsageDaily
    from app.services.quota import month_start_local

    start = month_start_local()
    usage_rows = (
        await db.execute(
            select(UsageDaily.org_id, func.coalesce(func.sum(UsageDaily.questions), 0))
            .where(UsageDaily.day >= start)
            .group_by(UsageDaily.org_id)
        )
    ).all()
    usage_map = {int(r[0]): int(r[1] or 0) for r in usage_rows}

    quota_rows = (await db.execute(select(OrgQuota))).scalars().all()
    quota_map = {q.org_id: q for q in quota_rows}
    return usage_map, quota_map


def _org_out(org, member_count: int, kb_count: int, usage_map, quota_map):
    """把组织 + 统计 + 用量额度拼成对外结构（列表与单查共用，避免两处写法不一致）。"""
    q = quota_map.get(org.id)
    return OrgOut(
        id=org.id, name=org.name, description=org.description,
        is_active=org.is_active, created_at=org.created_at,
        member_count=member_count, kb_count=kb_count,
        monthly_questions=usage_map.get(org.id, 0),
        monthly_question_limit=q.monthly_question_limit if q else None,
        question_limit_per_day=q.daily_question_limit if q else None,
        is_service_enabled=q.is_service_enabled if q else True,
    )


@router.get("/me", response_model=OrgOut)
async def get_my_organization(current_user: CurrentUser, db: DbSession):
    """查看自己所属的组织（前端用来显示"当前在哪个组织下工作"）。"""
    if current_user.org_id is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "当前账号未归属任何组织。请联系平台管理员把你加入一个组织。",
        )
    org = await db.get(Organization, current_user.org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")
    m, k = await _counts(db, org.id)
    usage_map, quota_map = await _usage_and_quota_maps(db)
    return _org_out(org, m, k, usage_map, quota_map)


@router.get("/me/invite-code", response_model=InviteCodeOut)
async def get_my_invite_code(current_user: CurrentUser, db: DbSession):
    """查看**自己所属组织**的邀请码：平台超管或本组织管理员可用。

    为什么要开这个口子（原先设计是"邀请码只在创建时返回一次"，实际不可用）：
        组织管理员是本组织唯一能拉人进来的人。如果他看不到邀请码，
        每次招一个同事都得找平台运营重置一次码，现实中没法用。
    为什么风险可控：
        这个码只能把人拉进"他自己已经在管的那个组织"，不产生任何跨组织权限；
        成员数量上限由计费/额度模块兜住，不会无限膨胀。
    为什么普通成员不给看：
        普通成员没有管理职责，给他看只会扩大凭证的知情面。
    """
    if current_user.org_id is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "当前账号未归属任何组织，无法查看邀请码"
        )
    is_super = is_superadmin(current_user)
    if not (is_super or current_user.role == "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有组织管理员才能查看邀请码")

    org = await db.get(Organization, current_user.org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")
    return InviteCodeOut(org_id=org.id, invite_code=org.invite_code)


@router.get("", response_model=list[OrgOut])
async def list_organizations(superadmin: SuperAdminUser, db: DbSession):
    """列出全部组织（平台超管），并带上本月用量与额度，方便一眼看出谁快用完了。"""
    orgs = (await db.execute(select(Organization).order_by(Organization.created_at.desc()))).scalars().all()
    usage_map, quota_map = await _usage_and_quota_maps(db)
    # 成员数/知识库数一次批量取回（不是循环里逐个查，避免 N+1）
    members_map, kbs_map = await _counts_bulk(db)
    out: list[OrgOut] = []
    for o in orgs:
        m = members_map.get(o.id, 0)
        k = kbs_map.get(o.id, 0)
        out.append(_org_out(o, m, k, usage_map, quota_map))
    return out


@router.post("", response_model=OrgCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_organization(payload: OrgCreate, superadmin: SuperAdminUser, db: DbSession):
    """新建组织（平台超管）。返回的 invite_code 只在这里出现一次，请立刻保存。"""
    exists = await db.scalar(select(Organization).where(Organization.name == payload.name))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "同名组织已存在")

    # 邀请码用密码学随机数：不能用可猜的规律值，否则等于没有准入控制
    org = Organization(
        name=payload.name,
        description=payload.description,
        invite_code=_new_invite_code(),
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    logger.info("已创建组织：%s（id=%d）", org.name, org.id)
    return OrgCreatedOut(
        id=org.id, name=org.name, description=org.description,
        is_active=org.is_active, created_at=org.created_at, invite_code=org.invite_code,
    )


@router.put("/{org_id}", response_model=OrgOut)
async def update_organization(
    org_id: int, payload: OrgUpdate, superadmin: SuperAdminUser, db: DbSession
):
    """修改组织名称/备注/启停（平台超管）。

    停用一个组织后，其成员仍能登录，但知识库列表为空（因为隔离逻辑只按 org_id 过滤，
    组织自身的 is_active 用于运营侧标记，不参与鉴权判断，避免登录态突变的意外）。
    """
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")

    if payload.name is not None and payload.name != org.name:
        dup = await db.scalar(select(Organization).where(Organization.name == payload.name))
        if dup:
            raise HTTPException(status.HTTP_409_CONFLICT, "同名组织已存在")
        org.name = payload.name
    if payload.description is not None:
        org.description = payload.description
    if payload.is_active is not None:
        org.is_active = payload.is_active

    db.add(org)
    await db.commit()
    await db.refresh(org)
    m, k = await _counts(db, org.id)
    usage_map, quota_map = await _usage_and_quota_maps(db)
    return _org_out(org, m, k, usage_map, quota_map)


@router.post("/{org_id}/rotate-invite", response_model=InviteCodeOut)
async def rotate_invite_code(org_id: int, superadmin: SuperAdminUser, db: DbSession):
    """重置组织邀请码（平台超管）。

    典型场景：邀请码被不小心发到群里了，想立刻作废重发。旧码会立即失效。
    """
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")
    org.invite_code = _new_invite_code()
    db.add(org)
    await db.commit()
    logger.info("组织 %s（id=%d）的邀请码已重置", org.name, org.id)
    return InviteCodeOut(org_id=org.id, invite_code=org.invite_code)


@router.get("/{org_id}/members", response_model=list[UserOut])
async def list_members(org_id: int, current_user: CurrentUser, db: DbSession):
    """查看组织成员：平台超管可看任意组织；组织管理员只能看自己组织。"""
    is_super = is_superadmin(current_user)
    is_own_org_admin = current_user.role == "admin" and current_user.org_id == org_id
    if not (is_super or is_own_org_admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能查看自己所在组织的成员")

    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")

    rows = (
        (await db.execute(select(User).where(User.org_id == org_id).order_by(User.id)))
        .scalars()
        .all()
    )
    return rows


def _new_invite_code() -> str:
    """生成一个新的组织邀请码。"""
    return secrets.token_hex(8)
