"""向量化封装：调用阿里云百炼 text-embedding-v4 把文本批量变成向量。

- 走 OpenAI 兼容协议（AsyncOpenAI 客户端 + 百炼 base_url）；
- 自动分批（batch_size）、失败指数退避重试、分批间限速，避免触发供应商 QPS 限制；
- 超时/重试次数等参数全部来自配置。
"""

import asyncio
import logging

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger("kbqa.vectorizer")

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """惰性创建百炼客户端（async）。"""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            timeout=settings.embedding_timeout,
            max_retries=0,  # 重试由下面自实现（可控退避），不依赖 SDK 默认
        )
    return _client


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """把一段段文本转成向量，返回与输入顺序一一对应的向量列表。"""
    if not texts:
        return []
    client = get_client()
    results: list[list[float]] = []
    batch_size = settings.embedding_batch_size

    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vecs = await _embed_batch_with_retry(client, batch)
        results.extend(vecs)
        # 分批之间稍微停顿，礼貌限速（text-embedding-v4 有 QPS 配额）
        if start + batch_size < len(texts):
            await asyncio.sleep(0.15)
    return results


async def _embed_batch_with_retry(client: AsyncOpenAI, batch: list[str]) -> list[list[float]]:
    """单批向量化，失败时指数退避重试（默认最多 3 次）。"""
    last_err: Exception | None = None
    for attempt in range(settings.embedding_max_retries + 1):
        try:
            resp = await client.embeddings.create(
                model=settings.embedding_model,
                input=batch,
                dimensions=settings.embedding_dimensions,
            )
            # 接口不保证返回顺序，按 index 排回输入顺序
            rows = sorted(resp.data, key=lambda d: d.index)
            return [r.embedding for r in rows]
        except Exception as e:  # 网络抖动 / 5xx / 限流，按 2^n 秒退避重试
            last_err = e
            if attempt < settings.embedding_max_retries:
                wait = 2**attempt
                logger.warning("向量化第 %d 批失败（%s），%.1f 秒后重试…", attempt + 1, e, wait)
                await asyncio.sleep(wait)
    raise RuntimeError(f"文本向量化失败（已重试 {settings.embedding_max_retries} 次）：{last_err}")


async def health_check() -> tuple[bool, str]:
    """用一句固定文本试向量化，验证 Key/网络/模型可用（管理员页面状态展示）。"""
    try:
        vecs = await embed_texts(["健康检查"])
        dim = len(vecs[0]) if vecs else 0
        return True, f"正常（返回维度 {dim}）"
    except Exception as e:
        return False, str(e)[:200]
