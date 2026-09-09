"""Qdrant 向量库封装：集合管理 / 批量写入 / 按文档删除 / 检索。

设计约定：
- 所有知识库共用一个集合（同一 embedding 模型、同维度），payload 带 kb_id/doc_id 过滤；
- 向量点 ID 用 d{doc_id}-c{chunk_index}，同一文档重传时先按 doc_id 删除旧点再写入；
- kb_id / doc_id 建了 payload 索引，过滤删除/检索都很快。
"""

import logging
import uuid

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.core.config import settings

logger = logging.getLogger("kbqa.vector_store")

_client: AsyncQdrantClient | None = None


def get_client() -> AsyncQdrantClient:
    """惰性创建全局 Qdrant 客户端（async）。"""
    global _client
    if _client is None:
        _client = AsyncQdrantClient(url=settings.qdrant_url)
    return _client


async def close_client() -> None:
    """应用关闭时释放连接。"""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def ensure_collection() -> None:
    """确保集合存在；不存在则创建（维度取配置的向量维度，余弦距离），并建过滤索引。"""
    client = get_client()
    try:
        if not await client.collection_exists(settings.qdrant_collection):
            await client.create_collection(
                collection_name=settings.qdrant_collection,
                vectors_config=VectorParams(
                    size=settings.embedding_dimensions, distance=Distance.COSINE
                ),
            )
            logger.info("已创建 Qdrant 集合 %s（维度 %d）", settings.qdrant_collection, settings.embedding_dimensions)
    except Exception as e:  # 集合已存在等竞态，记录但继续
        logger.warning("ensure_collection 检查集合时出现异常：%s", e)
    # 为按库/按文档过滤建关键字索引（幂等，已存在会抛错，忽略即可）
    for field in ("kb_id", "doc_id"):
        try:
            await client.create_payload_index(
                settings.qdrant_collection, field, PayloadSchemaType.INTEGER
            )
        except Exception:
            pass


def _point_id(doc_id: int, chunk_index: int) -> str:
    """生成确定性的 UUID 作为向量点编号（Qdrant 只接受整数或 UUID）。

    同一文档同一片段编号永远映射同一个 UUID：重传覆盖/断点续传时幂等写入。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"doc-{doc_id}-chunk-{chunk_index}"))


async def upsert_document_chunks(
    kb_id: int, doc_id: int, doc_name: str, vectors: list[list[float]], texts: list[str], pages: list[int | None]
) -> int:
    """把一个文档的全部分块向量批量写入 Qdrant（每 500 点一批）。返回写入点数。

    vectors[i] 与 texts[i]/pages[i] 一一对应（由入库管线保证顺序一致）。
    """
    client = get_client()
    total = len(vectors)
    if total == 0:
        return 0
    batch_size = 500
    written = 0
    for start in range(0, total, batch_size):
        points: list[PointStruct] = []
        for i in range(start, min(start + batch_size, total)):
            points.append(
                PointStruct(
                    id=_point_id(doc_id, i),
                    vector=vectors[i],
                    payload={
                        "kb_id": kb_id,
                        "doc_id": doc_id,
                        "doc_name": doc_name,
                        "chunk_index": i,
                        "page": pages[i],
                        "text": texts[i],
                    },
                )
            )
        await client.upsert(collection_name=settings.qdrant_collection, points=points)
        written += len(points)
    logger.info("文档 %s 入库完成，共写入 %d 个向量点", doc_name, written)
    return written


async def delete_document_chunks(doc_id: int) -> int:
    """按 doc_id 删除某文档的全部向量点（删文档 / 重传覆盖前调用）。返回删除点数。"""
    client = get_client()
    res = await client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=Filter(
            must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
        ),
    )
    return getattr(res, "status", None)


async def delete_kb_chunks(kb_id: int) -> None:
    """删除某知识库的全部向量点（删除知识库时调用）。"""
    client = get_client()
    await client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=Filter(
            must=[FieldCondition(key="kb_id", match=MatchValue(value=kb_id))]
        ),
    )


async def count_kb_chunks(kb_id: int) -> int:
    """统计某知识库的向量点总数（管理页展示）。"""
    client = get_client()
    try:
        res = await client.count(
            collection_name=settings.qdrant_collection,
            count_filter=Filter(
                must=[FieldCondition(key="kb_id", match=MatchValue(value=kb_id))]
            ),
            exact=True,
        )
        return res.count or 0
    except Exception:
        return 0


async def list_document_chunks(doc_id: int, limit: int = 200) -> list[dict]:
    """按 doc_id 读取向量点（分块预览用），按 chunk_index 排序返回。"""
    client = get_client()
    try:
        res = await client.scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=Filter(
                must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
            ),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
    except Exception:
        return []
    items = []
    for p in res[0]:
        pl = p.payload or {}
        items.append(
            {
                "chunk_index": pl.get("chunk_index"),
                "page": pl.get("page"),
                "text": pl.get("text", ""),
            }
        )
    items.sort(key=lambda x: (x["chunk_index"] is None, x["chunk_index"] or 0))
    return items


async def search(
    query_vector: list[float],
    kb_ids: list[int] | None = None,
    top_k: int | None = None,
    score_threshold: float | None = None,
) -> list[dict]:
    """语义检索：在向量库中找与 query_vector 最相似的片段。

    Args:
        query_vector: 问题向量化后的向量。
        kb_ids: 限定检索的知识库 id 列表；None=检索全部（用户未选库时）。
        top_k: 取回候选片段数，默认用配置 retrieval_top_k。
        score_threshold: 相似度下限，低于该值的片段被丢弃；默认用配置。

    Returns:
        按相似度从高到低排列的命中列表，每项含：
        kb_id / doc_id / doc_name / chunk_index / page / text / score。
    """
    client = get_client()
    top_k = top_k or settings.retrieval_top_k
    threshold = settings.retrieval_score_threshold if score_threshold is None else score_threshold

    must: list = []
    if kb_ids:
        must.append(
            FieldCondition(
                key="kb_id",
                match=MatchValue(value=kb_ids[0] if len(kb_ids) == 1 else kb_ids),
            )
        )
    qfilter = Filter(must=must) if must else None

    res = await client.query_points(
        collection_name=settings.qdrant_collection,
        query=query_vector,
        query_filter=qfilter,
        limit=top_k,
        with_payload=True,
        with_vectors=False,
    )
    hits: list[dict] = []
    for p in res.points:
        score = float(p.score or 0.0)
        if score < threshold:
            continue  # 相似度过低：视为未命中，不给模型（避免硬答）
        pl = p.payload or {}
        hits.append(
            {
                "kb_id": pl.get("kb_id"),
                "doc_id": pl.get("doc_id"),
                "doc_name": pl.get("doc_name", ""),
                "chunk_index": pl.get("chunk_index"),
                "page": pl.get("page"),
                "text": pl.get("text", ""),
                "score": round(score, 4),
            }
        )
    return hits
