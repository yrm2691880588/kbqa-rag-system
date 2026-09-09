"""用户相关的接口数据结构：注册/登录/改密/用户信息。"""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class UserRegister(BaseModel):
    """注册请求体：用户名 + 密码 + 可选昵称。"""

    username: str = Field(
        min_length=3,
        max_length=32,
        pattern=r"^[a-zA-Z0-9_]+$",
        description="登录名：3-32位字母/数字/下划线",
    )
    password: str = Field(min_length=6, max_length=64, description="密码至少 6 位")
    nickname: str = Field(default="", max_length=32, description="昵称（可选）")


class LoginRequest(BaseModel):
    """登录请求体。"""

    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    """修改密码请求体：必须带旧密码验证。"""

    old_password: str = Field(min_length=6, max_length=64)
    new_password: str = Field(min_length=6, max_length=64)


class UserOut(BaseModel):
    """对外返回的用户信息（绝不包含密码哈希）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nickname: str
    role: str
    is_active: bool
    created_at: dt.datetime
    last_login_at: dt.datetime | None = None
