"""健康检查端点：供容器编排、负载均衡与监控系统轮询。

四个探针各司其职：
  GET /health/live   存活探针：只证明进程还在跑（不碰任何依赖）→ 容器重启判据
  GET /health/ready  就绪探针：依赖是否可用（数据库/向量库/磁盘）→ 是否放流量进来
  GET /health        基础探活：同就绪探针，返回完整分项明细（人看/监控看都行）
  GET /health/deep   深度探活：额外真实调用外部 AI 服务，验证密钥与网络（低频调用）

状态码约定：健康 → 200；不健康 → 503（监控可直接靠状态码判警，无需解析响应体）。
"""

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.core.deps import AdminUser
from app.services.rate_limit_base import BaseKeyedLimiter
from app.services.health_check import run_checks

router = APIRouter(prefix="/health", tags=["健康检查"])


class _SlidingWindowLimiter(BaseKeyedLimiter):
    """按 key 的滑动窗口限流（进程内内存实现）。

    为什么用滑动窗口而不是"固定每分钟清零"：
        固定窗口在边界上会放过 2 倍流量（第 59 秒来一批、第 61 秒又来一批，
        两批都不受限）。滑动窗口记录每次调用的时间戳，任何时刻都严格
        "最近 N 秒内不超过 M 次"。

    为什么放在进程内：这个端点一天调用不了几次，没必要引入 Redis。
    多进程部署时每个进程各自计数，实际总量是 N×进程数 —— 对"防误刷"够用。
    """

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        super().__init__()
        self.max_calls = max_calls
        self.window = window_seconds

    def hit(self, key: str) -> float:
        """记一次调用。允许则返回 0；超限则返回"还要等几秒"。"""
        now = self._now()
        bucket = list(self._get_state(key) or [])
        # 丢掉窗口外的旧记录
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= self.max_calls:
            left = max(0.0, self.window - (now - bucket[0]))
            self._set_state(key, bucket, now)
            return left
        bucket.append(now)
        self._set_state(key, bucket, now)
        return 0.0


# 深度探活：每人每分钟最多 3 次（每次都会真实调用付费接口）
_deep_probe_limiter = _SlidingWindowLimiter(max_calls=3, window_seconds=60)

# 不健康时返回的状态码（负载均衡/监控据此摘除流量）
_UNHEALTHY = 503


@router.get("")
async def health(response: Response):
    """基础探活：数据库 + 向量库 + 上传目录 + AI 服务配置。不消耗外部额度，可高频轮询。"""
    data = await run_checks(deep=False)
    if not data["healthy"]:
        response.status_code = _UNHEALTHY
    return data


@router.get("/live")
async def live():
    """存活探针：进程能响应即为存活（不检查依赖，避免依赖抖动导致容器被反复重启）。"""
    return {"status": "ok"}


@router.get("/ready")
async def ready(response: Response):
    """就绪探针：依赖不可用时返回 503，供编排/负载均衡摘除流量。"""
    data = await run_checks(deep=False)
    if not data["healthy"]:
        response.status_code = _UNHEALTHY
    return data


@router.get("/deep")
async def deep(
    response: Response,
    admin: AdminUser,
    llm: bool = Query(False, description="是否也真实调用一次对话模型（会产生极少量费用）"),
):
    """深度探活：真实调用向量服务（可选对话模型），用于验证密钥/网络确实可用。

    建议人工排查或定时巡检（如每小时一次）时使用，不要用于秒级轮询。

    ⚠️ 为什么必须要求管理员登录：
       这个端点会真的去调百炼/DeepSeek 接口，**每次都要花钱**。
       如果它像 /live 那样对所有人开放，任何人都能反复刷它把你的配额烧光。
       真正需要无人值守调用的只有 /live 与 /ready，那两个不碰外部服务，保持匿名。

    ⚠️ 为什么还要限流：
       只要求登录还不够 —— 管理员账号本身也可能被拿到（共用电脑没退出、
       密码泄露），或者自己写了个脚本按秒轮询。这个端点一次要调两个付费接口，
       刷起来照样能把额度烧光。所以按"每分钟最多几次"兜一层。
       超限直接 429，并明确告诉对方多久后可以再试。
    """
    wait = _deep_probe_limiter.hit(str(admin.id))
    if wait > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"深度探活调用过于频繁（每次都会产生费用），请 {int(wait) + 1} 秒后再试",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    data = await run_checks(deep=True, llm_call=llm)
    if not data["healthy"]:
        response.status_code = _UNHEALTHY
    return data
