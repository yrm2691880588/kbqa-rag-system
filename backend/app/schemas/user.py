"""用户相关的接口数据结构：注册/登录/改密/用户信息/短信验证码。"""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.security import validate_password_strength

# 中国大陆手机号格式（1 开头，第二位 3-9，共 11 位）
_PHONE_PATTERN = r"^1[3-9]\d{9}$"


class UserRegister(BaseModel):
    """注册请求体：用户名 + 密码 + 图形验证码（防机器人批量注册）。"""

    username: str = Field(
        min_length=3,
        max_length=32,
        pattern=r"^[a-zA-Z0-9_]+$",
        description="登录名：3-32位字母/数字/下划线",
    )
    password: str = Field(min_length=8, max_length=64, description="密码至少 8 位且含字母数字")
    nickname: str = Field(default="", max_length=32, description="昵称（可选）")
    # 人机校验：先调 GET /auth/captcha 拿到 captcha_id 和图片，答对才能注册
    captcha_id: str = Field(min_length=8, description="图形验证码编号（取图时下发）")
    captcha_code: str = Field(min_length=1, max_length=8, description="图形验证码内容")

    @model_validator(mode="after")
    def _check_password(self) -> "UserRegister":
        """注册密码必须过强度门槛（8位+字母数字+非弱密码+不含用户名）。"""
        ok, reason = validate_password_strength(self.password, self.username)
        if not ok:
            raise ValueError(reason)
        return self


class LoginRequest(BaseModel):
    """账号密码登录请求体。"""

    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=64)


class ChangePasswordRequest(BaseModel):
    """修改密码请求体：必须带旧密码验证；新密码强度在接口层按当前用户名校验。"""

    old_password: str = Field(min_length=1, max_length=64)
    new_password: str = Field(min_length=8, max_length=64)


# ============ 图形验证码 ============

class CaptchaOut(BaseModel):
    """图形验证码响应：前端拿 captcha_id + 图片展示给用户。"""

    captcha_id: str
    image_base64: str
    expires_in: int  # 有效期（秒）


# ============ 短信验证码 ============

class SmsSendRequest(BaseModel):
    """申请发送短信验证码：必须先答对图形验证码（防短信轰炸）。"""

    phone: str = Field(pattern=_PHONE_PATTERN, description="中国大陆手机号")
    captcha_id: str = Field(min_length=8)
    captcha_code: str = Field(min_length=1, max_length=8)


class SmsSendOut(BaseModel):
    """发码结果。dev_code 仅在测试模式（SMS_PROVIDER=mock）返回。"""

    message: str
    dev_code: str | None = None
    retry_after: int = 60  # 下次可发送的等待秒数


class SmsLoginRequest(BaseModel):
    """手机号 + 短信验证码 登录/注册 请求体。"""

    phone: str = Field(pattern=_PHONE_PATTERN)
    code: str = Field(min_length=4, max_length=8)


class BindPhoneRequest(BaseModel):
    """已登录用户绑定手机号（绑定后该手机号验证码登录即此账号）。"""

    phone: str = Field(pattern=_PHONE_PATTERN)
    code: str = Field(min_length=4, max_length=8)


class UserOut(BaseModel):
    """对外返回的用户信息（绝不包含密码哈希）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nickname: str
    role: str
    is_active: bool
    phone: str | None = None
    created_at: dt.datetime
    last_login_at: dt.datetime | None = None
