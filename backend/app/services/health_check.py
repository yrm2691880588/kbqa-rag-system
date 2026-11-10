"""健康探活服务：把"服务到底能不能用"拆成可独立判定的若干项。

为什么需要它（对运维的实际意义）：
  - 只探数据库是不够的：向量库断了、AI 密钥失效了，接口照样能"在线"但对用户完全没用；
  - 监控系统需要一个**纯进程存活**的探针（容器重启判据）与一个**依赖就绪**的探针（流量调度判据）；
  - 排障时需要能区分"本地依赖问题"与"外部服务问题"，因此分项返回明细与耗时。

分级设计：
  - 基础探活：只探本地/内网依赖（数据库、向量库、磁盘），不消耗任何外部额度，可高频轮询；
  - 深度探活：额外**真实调用**一次向量服务（成本可忽略）与对话模型（可选），验证密钥与网络确实可用。

健壮性：单项探活都有超时与异常兜底，任何一项卡住/报错都不会拖垮整个探活请求。
"""

import asyncio
import logging
import time

import httpx
from sqlalchemy import text

from app.core.config import settings

logger = logging.getLogger("kbqa.health")

# 单项探活超时（秒）：超过即判为异常，避免探活自身被慢依赖拖死。
# 从配置读（P2-20）：设太小会在数据库偶尔卡顿时误报"不健康"，
# 设太大会让监控系统的探活请求自己超时（监控一般给 10 秒）。
def _check_timeout() -> float:
    return float(getattr(settings, "health_check_timeout", 5.0) or 5.0)


def _result(name: str, ok: bool, detail: str, cost_ms: int) -> dict:
    """统一的单项探活结果结构。"""
    return {"name": name, "ok": ok, "detail": detail, "cost_ms": cost_ms}


async def _run(name: str, checker) -> dict:
    """执行单项探活：统一计时，并把超时/异常都转成"不健康"结果（而不是抛出去）。"""
    start = time.perf_counter()
    timeout = _check_timeout()
    try:
        ok, detail = await asyncio.wait_for(checker(), timeout=timeout)
    except asyncio.TimeoutError:
        ok, detail = False, f"探活超时（超过 {timeout:.0f} 秒未响应）"
    except Exception as e:  # 探活本身绝不能把请求打挂
        logger.warning("健康探活项 %s 异常：%s", name, e)
        ok, detail = False, f"探活异常：{type(e).__name__}: {str(e)[:150]}"
    return _result(name, ok, detail, int((time.perf_counter() - start) * 1000))


# ===== 各项探活的具体实现 =====

async def _check_database() -> tuple[bool, str]:
    """数据库：独立开会话探活，确保即使连不上也能给出结论而不是 500。

    返回的信息里带上**连接池实时状态**（P2-17）。为什么这个数很重要：
      · "正在使用中的连接数"如果一直贴着上限下不来，就说明有害虫——
        某个地方借了连接没还（异常路径最容易漏）。这种问题靠等故障报警发现太晚；
      · 把它放进健康检查，监控上就能看到趋势，提前发现"池子在慢慢被耗干"。
    """
    from app.db.session import AsyncSessionLocal, pool_status

    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT 1"))

    st = pool_status()
    if not st:
        return True, "连接正常（当前数据库不支持查看连接池状态）"
    detail = (
        f"连接正常；连接池 {st['checked_out']}/{st['size']}"
        f"（在用/常备），可超借 {st['overflow']} 条"
    )
    # 借出去的已经超过常备数量 → 说明正处在高峰（或有人没还连接），值得在监控上盯一下
    if st["size"] and st["checked_out"] >= st["size"]:
        detail += " ⚠️ 已用满常备连接，正在超借（若持续如此请检查是否有连接未归还）"
    return True, detail


async def _check_qdrant() -> tuple[bool, str]:
    """向量库：既要连得上，也要确认集合已建（否则首次入库会失败）。"""
    from app.services.vector_store import get_client

    client = get_client()
    collections = await client.get_collections()
    names = [c.name for c in collections.collections]
    if settings.qdrant_collection not in names:
        return False, f"连接正常，但集合 {settings.qdrant_collection} 不存在（尚未初始化）"
    info = await client.get_collection(settings.qdrant_collection)
    return True, f"连接正常，集合 {settings.qdrant_collection} 就绪（{info.points_count} 个向量点）"


async def _check_upload_dir() -> tuple[bool, str]:
    """上传目录：确认存在且可写（磁盘写满/权限错误在这里就能暴露）。"""
    from app.services.ingestion import UPLOAD_ROOT

    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    probe = UPLOAD_ROOT / ".healthcheck"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return True, f"目录可写（{UPLOAD_ROOT}）"


async def _check_llm_config() -> tuple[bool, str]:
    """对话模型：只查配置是否就绪（不调用，零成本）。"""
    if not settings.llm_api_key.strip():
        return False, "LLM_API_KEY 未配置"
    if settings.llm_mock:
        return True, f"已配置（{settings.llm_model}），当前为压测模拟模式（不会真实调用）"
    return True, f"已配置（{settings.llm_model}）"


async def _check_embedding_config() -> tuple[bool, str]:
    """向量服务：只查配置是否就绪（不调用，零成本）。"""
    if not settings.embedding_api_key.strip():
        return False, "EMBEDDING_API_KEY 未配置"
    return True, f"已配置（{settings.embedding_model}）"


async def _check_embedding_call() -> tuple[bool, str]:
    """向量服务：真实调用一次（验证密钥/网络/模型可用，成本可忽略）。"""
    from app.services import vectorizer

    if not settings.embedding_api_key.strip():
        return False, "EMBEDDING_API_KEY 未配置"
    if settings.llm_mock:
        return True, "压测模拟模式：跳过真实调用"
    ok, message = await vectorizer.health_check()
    return ok, message


async def _check_llm_call() -> tuple[bool, str]:
    """对话模型：真实调用一次（会产生极少量费用，仅在显式请求时执行）。"""
    from openai import AsyncOpenAI

    if not settings.llm_api_key.strip():
        return False, "LLM_API_KEY 未配置"
    if settings.llm_mock:
        return True, "压测模拟模式：跳过真实调用"
    client = AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        # 探活这一步的超时，比正常问答宽松一点：只发一个 "ping"，
        # 但首次建连（TLS 握手、DNS）有时偏慢，卡太紧会误报"模型不可用"。
        timeout=httpx.Timeout(
            float(getattr(settings, "health_llm_probe_timeout", 15.0) or 15.0),
            connect=float(getattr(settings, "llm_connect_timeout", 15.0) or 15.0),
        ),
        max_retries=0,
    )
    try:
        resp = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        used = getattr(resp, "usage", None)
        tokens = f"，消耗 {used.total_tokens} token" if used else ""
        return True, f"调用成功（模型 {settings.llm_model}{tokens}）"
    finally:
        await client.close()


# ===== 对外入口 =====

async def run_checks(*, deep: bool = False, llm_call: bool = False) -> dict:
    """执行一组探活并汇总。

    Args:
        deep: 是否深度探活（真实调用外部 AI 服务）。
        llm_call: 深度探活时是否也真实调用对话模型（会消耗极少量费用）。
    Returns:
        汇总字典：status / healthy / env / checks 明细。
    """
    tasks = [
        _run("数据库", _check_database),
        _run("向量库（Qdrant）", _check_qdrant),
        _run("上传目录", _check_upload_dir),
        _run("对话模型（配置）", _check_llm_config),
    ]
    if deep:
        tasks.append(_run("向量服务（真实调用）", _check_embedding_call))
        if llm_call:
            tasks.append(_run("对话模型（真实调用）", _check_llm_call))
    else:
        tasks.append(_run("向量服务（配置）", _check_embedding_config))

    checks = await asyncio.gather(*tasks)
    healthy = all(c["ok"] for c in checks)
    return {
        "status": "ok" if healthy else "unhealthy",
        "healthy": healthy,
        "env": settings.app_env,
        "checks": list(checks),
    }
