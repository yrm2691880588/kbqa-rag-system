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
    """用刷新令牌换新令牌的请求体。

    refresh_token 改为可选：浏览器端刷新令牌放在 HttpOnly Cookie 里，
    请求体不需要（也不应该）再传一遍 —— JS 本来就读不到它。
    命令行/脚本没有 Cookie 环境时，仍然可以在请求体里传。
    """

    refresh_token: str | None = None


class LogoutRequest(BaseModel):
    """登出请求体。

    refresh_token 改为可选：Cookie 模式下服务端直接从 Cookie 取，
    浏览器端甚至可以不发请求体。脚本调用时仍可显式传。
    """

    refresh_token: str | None = None
