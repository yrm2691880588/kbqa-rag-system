"""令牌相关接口结构。"""

from pydantic import BaseModel

from app.schemas.user import UserOut


class TokenPair(BaseModel):
    """登录/刷新成功后的返回：双令牌 + 用户信息。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class RefreshRequest(BaseModel):
    """用刷新令牌换新令牌的请求体。"""

    refresh_token: str


class LogoutRequest(BaseModel):
    """登出请求体：带刷新令牌以便作废。"""

    refresh_token: str
