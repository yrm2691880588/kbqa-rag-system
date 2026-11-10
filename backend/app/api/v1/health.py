"""健康检查端点：供容器编排、负载均衡与监控系统轮询。

四个探针各司其职：
  GET /health/live   存活探针：只证明进程还在跑（不碰任何依赖）→ 容器重启判据
  GET /health/ready  就绪探针：依赖是否可用（数据库/向量库/磁盘）→ 是否放流量进来
  GET /health        基础探活：同就绪探针，返回完整分项明细（人看/监控看都行）
  GET /health/deep   深度探活：额外真实调用外部 AI 服务，验证密钥与网络（低频调用）

状态码约定：健康 → 200；不健康 → 503（监控可直接靠状态码判警，无需解析响应体）。
"""

from fastapi import APIRouter, Query, Response

from app.core.deps import AdminUser
from app.services.health_check import run_checks

router = APIRouter(prefix="/health", tags=["健康检查"])

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
    """
    data = await run_checks(deep=True, llm_call=llm)
    if not data["healthy"]:
        response.status_code = _UNHEALTHY
    return data
