"""用户接口：查看自己的信息 / 修改密码 / 绑定手机号 / 管理员管理成员。

分成两块：
    /me 系列        —— 任何登录用户都能操作自己的账号；
    其余    —— 组织管理员管**本组织**成员，平台超管管任意组织。
                       一条铁律：任何"按 user_id 操作"的接口，都必须先确认
                       目标用户与自己同组织，否则就是把别人公司的人给停了。
"""

import logging
import secrets

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select, update

from app.core.deps import AdminUser, CurrentUser, DbSession
from app.core.security import hash_password, validate_password_strength, verify_password
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.user import (
    AdminResetPasswordOut,
    AdminUserUpdate,
    BindPhoneRequest,
    ChangePasswordRequest,
    UserOut,
)
from app.services import auth_guard

logger = logging.getLogger("kbqa.api.users")

router = APIRouter(prefix="/users", tags=["用户"])

# 组织内允许的角色。**故意不含 superadmin**：
# 平台超管是"系统运营方"的身份，属于跨组织的角色。
# 如果允许通过这个接口授予，任何组织管理员都能把自己变成平台超管，
# 那道"超管才能建组织"的墙就塌了。
ORG_ROLES = ("user", "admin")


@router.get("/me", response_model=UserOut)
async def get_me(current_user: CurrentUser):
    """获取当前登录用户的资料（前端登录后初始化用）。"""
    return current_user


@router.put("/me/password")
async def change_password(payload: ChangePasswordRequest, db: DbSession, current_user: CurrentUser):
    """修改自己的密码：先验证旧密码，新密码需通过强度校验（与注册同一套规则）。"""
    if not verify_password(payload.old_password, current_user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "旧密码不正确")
    if payload.old_password == payload.new_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码不能与旧密码相同")

    ok, reason = validate_password_strength(payload.new_password, current_user.username)
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, reason)

    current_user.password_hash = hash_password(payload.new_password)
    db.add(current_user)
    await db.commit()
    return {"message": "密码修改成功，下次请用新密码登录"}


@router.post("/me/bind-phone")
async def bind_phone(payload: BindPhoneRequest, db: DbSession, current_user: CurrentUser):
    """绑定手机号：绑定后该手机号可用短信验证码登录，登录到的就是这个账号。

    这是"多端登录同一账号"的根基：将来微信/QQ 接入时，把它们的手机号绑到同一
    账号即可实现"同一手机号的微信/QQ/验证码 登录的都是同一个账号"。
    """
    # 校验短信验证码（同时作废，防重放）
    if not await auth_guard.verify_sms_code(db, payload.phone, payload.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码错误或已过期，请重新获取")

    # 手机号可能已被其他账号占用
    owner = await db.scalar(select(User).where(User.phone == payload.phone))
    if owner is not None and owner.id != current_user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "该手机号已绑定其他账号，无法重复绑定")

    current_user.phone = payload.phone
    db.add(current_user)
    await db.commit()
    return {"message": "手机号绑定成功，以后可用手机号验证码登录"}


# ============================================================
# 成员管理：组织管理员管本组织的人，平台超管管任意组织
# ============================================================


def _is_superadmin(user: User) -> bool:
    return bool(getattr(user, "is_superadmin", False))


async def _load_member_scoped(db, operator: User, user_id: int) -> User:
    """按 id 取一个成员，并确认"操作者有权动他"。

    这是本文件最重要的一个函数，所有管理动作都必须先过它。
    两层校验：
      ① 组织范围：组织管理员只能动**自己组织**的人；平台超管不受限；
      ② 保护超管：任何人都不能通过这里去动一个平台超管的账号
         （否则等于把平台运营方的账号交给客户处置）。
    越权一律 404，不返回 403 —— 403 等于告诉对方"这个账号存在，只是你没权限"。
    """
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")

    if _is_superadmin(target):
        # 平台超管的账号不接受组织侧管理
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")

    if _is_superadmin(operator):
        return target

    if operator.org_id is None or target.org_id != operator.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")
    return target


@router.get("", response_model=list[UserOut])
async def list_users(
    current_user: AdminUser,
    db: DbSession,
    org_id: int | None = Query(None, description="平台超管可指定组织；组织管理员忽略此项"),
    keyword: str | None = Query(None, max_length=64, description="按用户名/昵称/手机号模糊搜"),
    limit: int = Query(200, ge=1, le=1000),
):
    """成员列表。

    为什么带 keyword：客户几百人时，管理员找一个人靠翻页很不现实。
    """
    # 组织范围：组织管理员永远只能看自己的组织；超管可以指定，不指定就返回全部
    scope_org = org_id if _is_superadmin(current_user) else current_user.org_id
    if not _is_superadmin(current_user) and current_user.org_id is None:
        return []

    stmt = select(User).order_by(User.id)
    if scope_org is not None:
        stmt = stmt.where(User.org_id == scope_org)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            (User.username.ilike(like)) | (User.nickname.ilike(like)) | (User.phone.ilike(like))
        )
    stmt = stmt.limit(limit)
    return (await db.execute(stmt)).scalars().all()


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    payload: AdminUserUpdate,
    current_user: AdminUser,
    db: DbSession,
):
    """改成员角色 / 启停账号。

    三条保护（都是"出过一次就够呛"的场景）：
      ① 不能改自己 —— 顺手把自己降级或停用，就再也进不了管理页了；
      ② 不能把人提成平台超管 —— 提权通道封死在接口层（见 AdminUserUpdate）；
      ③ 停用账号时把它所有登录凭证一起作废 —— 否则那个人手里的登录状态
         还能继续用到令牌过期，等于"停了个寂寞"。
    """
    target = await _load_member_scoped(db, current_user, user_id)

    if target.id == current_user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能修改自己的角色或状态")

    changed: dict = {}

    if payload.role is not None and payload.role != target.role:
        target.role = payload.role
        changed["role"] = payload.role

    if payload.is_active is not None and payload.is_active != target.is_active:
        target.is_active = payload.is_active
        changed["is_active"] = payload.is_active
        if not payload.is_active:
            # 停用即刻踢下线：作废该用户所有未失效的刷新令牌
            await db.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == target.id, RefreshToken.revoked.is_(False))
                .values(revoked=True)
            )

    db.add(target)
    await db.commit()
    await db.refresh(target)
    if changed:
        logger.info(
            "管理员 %s 修改了成员 %s（id=%d）：%s",
            current_user.username, target.username, target.id, changed,
        )
    return target


@router.post("/{user_id}/reset-password", response_model=AdminResetPasswordOut)
async def admin_reset_password(user_id: int, current_user: AdminUser, db: DbSession):
    """管理员为成员重置密码（成员忘记密码且没绑手机号时的兜底通道）。

    为什么由系统生成随机密码、而不是让管理员自己设：
        管理员自己设密码，就等于管理员知道了别人的密码，能冒用对方身份。
        系统随机生成 + 一次性展示，责任边界清楚：管理员只是"传话的人"。
    重置后同样作废该用户所有登录凭证，并把新密码告诉本人，请其尽快自行修改。
    """
    target = await _load_member_scoped(db, current_user, user_id)

    if target.id == current_user.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "请使用「修改密码」功能（本接口用于帮他人重置）"
        )

    # 随机临时密码：字母 + 数字，长度 12，保证能过强度校验
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ"
    digits = "23456789"

    def _make() -> str:
        raw = (
            "".join(secrets.choice(alphabet) for _ in range(8))
            + "".join(secrets.choice(digits) for _ in range(4))
        )
        # 打乱顺序，避免"字母全在前"这种可猜的结构
        return "".join(secrets.choice(list(raw)) for _ in range(len(raw)))

    # ⚠️ 必须重试到"能过全站密码强度校验"为止。
    # 为什么不能只生成一次：强度规则里有一条"密码不能包含用户名"，
    # 而随机串恰好拼出用户名（如用户名 alice、密码里含 alice）是有概率的。
    # 一旦命中，管理员交出去的临时密码用户根本改不了密码——
    # 会被自己的临时密码卡住，只能再找管理员重置一次。
    # 生成出来的密码必须保证使用者能直接用，所以这里循环到合规为止。
    temp_password = _make()
    for _ in range(50):
        ok, _reason = validate_password_strength(temp_password, target.username)
        if ok:
            break
        temp_password = _make()

    target.password_hash = hash_password(temp_password)
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == target.id, RefreshToken.revoked.is_(False))
        .values(revoked=True)
    )
    db.add(target)
    await db.commit()
    logger.info(
        "管理员 %s 重置了成员 %s（id=%d）的密码，该成员旧登录状态已失效",
        current_user.username, target.username, target.id,
    )
    return AdminResetPasswordOut(
        message="已重置。请把临时密码当面或通过可靠渠道告诉本人，并请他登录后立即修改。",
        username=target.username,
        temp_password=temp_password,
    )
