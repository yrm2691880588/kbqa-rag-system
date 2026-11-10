"""令牌的 Cookie 存取（HttpOnly 方案的核心）。

为什么要改成 HttpOnly Cookie（大白话）：
    原来的做法是把 access_token / refresh_token 存进浏览器的 localStorage。
    localStorage 的问题在于：**任何 JavaScript 都能读它**。
    只要页面上有一处 XSS（比如某个 v-html 渲染了用户可控内容），
    攻击者一行 `localStorage.getItem('kbqa_tokens')` 就能把令牌偷走，
    然后拿你的账号去问问题、烧你的额度，直到令牌过期。

    换成 HttpOnly Cookie 后，浏览器会**自动带上**它，但 JS 读不到
    （document.cookie 里看不到 HttpOnly 的那几条）。即使有 XSS，
    攻击者也拿不到令牌原文。这是防"令牌被偷"的标准做法。

本文件负责三件事：
    ① 把令牌写进响应 Cookie（登录/刷新时）；
    ② 从请求里取出令牌（鉴权时，优先 Cookie，兼容旧的 Authorization 头）；
    ③ 退出登录时清除 Cookie。

Cookie 安全属性说明（每一个都不能省）：
    httponly=True   —— JS 读不到（防 XSS 偷令牌）
    secure          —— 只在 HTTPS 下发送（防明文链路被抓包）
    samesite        —— 防 CSRF。本项目前后端同源部署，用 lax 即可；
                       若将来前后端分域名，需要改成 none + secure
    path=/          —— 全站可用
"""

from __future__ import annotations

from fastapi import Request, Response

from app.core.config import settings

# Cookie 名称。加 kbqa_ 前缀，避免和同域其他应用冲突。
ACCESS_COOKIE = "kbqa_access"
REFRESH_COOKIE = "kbqa_refresh"


def _is_secure() -> bool:
    """是否给 Cookie 加 Secure 标记。

    生产环境必须开（只走 HTTPS）；本地开发是 http://127.0.0.1，
    如果也加 Secure，浏览器会直接不发送这个 Cookie，登录就"失效"了。
    所以按环境区分，而不是写死。
    """
    return settings.is_prod


def _cookie_kwargs(max_age: int) -> dict:
    return {
        "httponly": True,
        "secure": _is_secure(),
        "samesite": "lax",
        "path": "/",
        "max_age": max_age,
    }


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """把双令牌写进响应的 Cookie。"""
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        **_cookie_kwargs(settings.access_token_expire_minutes * 60),
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        **_cookie_kwargs(settings.refresh_token_expire_days * 24 * 3600),
    )


def clear_auth_cookies(response: Response) -> None:
    """清除令牌 Cookie（退出登录时调用）。"""
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(name, path="/")


def get_access_token(request: Request) -> str | None:
    """取访问令牌：优先 Cookie，回退到 Authorization 头。

    为什么保留 Authorization 回退：
        命令行脚本、压测工具、第三方调用方未必有 Cookie 环境，
        它们的标准做法就是带 Bearer 头。完全砍掉会让这些用法失效。
        而浏览器端优先用 Cookie（更安全）。
    """
    token = request.cookies.get(ACCESS_COOKIE)
    if token:
        return token
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def get_refresh_token(request: Request) -> str | None:
    """取刷新令牌：优先 Cookie，回退到请求体（由调用方另行处理）。"""
    return request.cookies.get(REFRESH_COOKIE) or None
