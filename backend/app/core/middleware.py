"""请求追踪中间件：给每个请求发一个编号，并让它贯穿所有日志。

做了什么（大白话）：
    请求进来时，先看看调用方有没有带编号（`X-Request-ID` 请求头）。
      · 带了 → 就用它。这样前端、网关、后端三边的日志能对上同一个编号；
      · 没带 → 我们自己生成一个。
    然后把这个编号放进"当前请求的上下文"，之后这个请求打的所有日志都会自动带上它，
    最后再把它放到响应头里返回给调用方 ——
    用户遇到问题时报出这个编号，运维一条命令就能捞出全过程。

为什么要用中间件而不是在每个接口里写：
    接口有几十个，靠人一个个加必然会漏；中间件是"所有请求的必经之路"，一处就够。
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging_setup import REQUEST_ID_HEADER, set_request_id

logger = logging.getLogger("kbqa.access")

# 这些路径不记访问日志（健康探活几秒钟一次，记下来会把日志刷满、
# 真正有用的信息被淹没 —— 这是运维最烦的一件事）
QUIET_PATHS = ("/health", "/health/ready", "/health/live", "/favicon.ico")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """给每个请求编号 + 记一条访问日志。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = set_request_id(request.headers.get(REQUEST_ID_HEADER, ""))
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # 即使接口炸了，也要留下"哪个请求炸了、花了多久"这条线索，
            # 否则用户报障时我们连查找的起点都没有
            cost = int((time.perf_counter() - started) * 1000)
            logger.exception("请求处理异常：%s %s（耗时 %dms）",
                             request.method, request.url.path, cost)
            raise

        cost = int((time.perf_counter() - started) * 1000)
        # 编号回给调用方：用户/前端可以把它一起报给客服
        response.headers[REQUEST_ID_HEADER] = rid

        path = request.url.path
        if not any(path.startswith(quiet) for quiet in QUIET_PATHS):
            # 慢请求单独标出来：排查"页面卡"时，一眼能看出是哪一类接口慢
            slow = " ⚠️慢请求" if cost >= 3000 else ""
            logger.info("%s %s → %d（%dms）%s",
                        request.method, path, response.status_code, cost, slow)

        return response
