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
    # 组织邀请码（可选）：填了就加入对应组织；不填则加入默认组织。
    # 为什么要邀请码而不是让用户自己选组织：组织名可以被猜到（挨个试"某某公司"），
    # 邀请码是随机串，猜不中，等于一道准入凭证。
    invite_code: str = Field(default="", max_length=32, description="组织邀请码（可选）")

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


# ============ 忘记密码 / 重置密码 ============

class ForgotPasswordRequest(BaseModel):
    """忘记密码第一步：用手机号申请一条重置短信验证码。

    为什么还要图形验证码：短信是要花钱的，而且能被当成骚扰工具。
    如果只凭手机号就能发，脚本可以拿这份接口当"短信轰炸机"用。
    """

    phone: str = Field(pattern=_PHONE_PATTERN, description="已绑定手机号")
    captcha_id: str = Field(min_length=8)
    captcha_code: str = Field(min_length=1, max_length=8)


class ResetPasswordRequest(BaseModel):
    """忘记密码第二步：凭短信验证码设置新密码。

    这里**不校验密码强度**（只卡长度），强度校验放到接口里做——
    因为强度规则需要"不能包含用户名"，而用户名只有在数据库里查到账号后才知道，
    入参模型这一层拿不到。在校验模型里硬做，会漏掉"密码里含用户名"这种情况。
    """

    phone: str = Field(pattern=_PHONE_PATTERN)
    code: str = Field(min_length=4, max_length=8)
    new_password: str = Field(min_length=8, max_length=64, description="新密码，至少 8 位")


class UserOut(BaseModel):
    """对外返回的用户信息（绝不包含密码哈希）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nickname: str
    role: str
    is_active: bool
    phone: str | None = None
    # 所属组织与是否平台超管：前端据此决定"要不要显示组织管理入口"
    org_id: int | None = None
    is_superadmin: bool = False
    created_at: dt.datetime
    last_login_at: dt.datetime | None = None


class SimpleMessageOut(BaseModel):
    """只回一句话的接口响应（重置密码、重置邀请码这类）。"""

    message: str


# ============ 管理员管理成员（P1-C）============

class AdminUserUpdate(BaseModel):
    """管理员修改成员：改角色 / 启停。

    两个字段都可选，只改传了的那个。
    role 只能填 user 或 admin —— **不能授予平台超管**，
    否则组织管理员可以给自己提权，跨组织隔离就形同虚设。
    """

    role: str | None = Field(default=None, description="user=普通成员 / admin=组织管理员")
    is_active: bool | None = Field(default=None, description="false=停用该账号，停用后无法登录")

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in ("user", "admin"):
            raise ValueError("角色只能是 user（普通成员）或 admin（组织管理员）")
        return v


class AdminResetPasswordOut(BaseModel):
    """管理员重置成员密码的返回。

    返回的临时密码**只在这里出现一次**：管理员把它当面/通过可靠渠道告诉本人，
    并要求本人首次登录后自行修改。系统不保存明文，事后谁也查不到。
    """

    message: str
    username: str
    temp_password: str


class RegisterPolicyOut(BaseModel):
    """公开的注册策略：前端据此决定「组织邀请码」是选填还是必填。

    为什么要求前端拿到这个开关：
        私有部署（只服务一家公司）时，同事自助注册不需要邀请码，输入框标成"选填"最省事；
        对外 SaaS 时则必须标成"必填"并给出说明，否则用户注册失败只会看到一句报错，很困惑。
    只暴露一个布尔值 —— 不返回默认组织名等任何组织信息，避免给扫描者提供线索。
    """

    invite_required: bool = False