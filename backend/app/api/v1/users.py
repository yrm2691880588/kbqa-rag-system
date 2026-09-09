"""用户接口：查看自己的信息 / 修改密码。"""

from fastapi import APIRouter, HTTPException, status

from app.core.deps import CurrentUser, DbSession
from app.core.security import hash_password, verify_password
from app.schemas.user import ChangePasswordRequest, UserOut

router = APIRouter(prefix="/users", tags=["用户"])


@router.get("/me", response_model=UserOut)
async def get_me(current_user: CurrentUser):
    """获取当前登录用户的资料（前端登录后初始化用）。"""
    return current_user


@router.put("/me/password")
async def change_password(payload: ChangePasswordRequest, db: DbSession, current_user: CurrentUser):
    """修改自己的密码：先验证旧密码，通过后才写入新密码的哈希。"""
    if not verify_password(payload.old_password, current_user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "旧密码不正确")
    if payload.old_password == payload.new_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码不能与旧密码相同")

    current_user.password_hash = hash_password(payload.new_password)
    db.add(current_user)
    await db.commit()
    return {"message": "密码修改成功，下次请用新密码登录"}
