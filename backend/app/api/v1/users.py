"""用户接口：查看自己的信息 / 修改密码 / 绑定手机号。"""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession
from app.core.security import hash_password, validate_password_strength, verify_password
from app.models.user import User
from app.schemas.user import BindPhoneRequest, ChangePasswordRequest, UserOut
from app.services import auth_guard

router = APIRouter(prefix="/users", tags=["用户"])


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
