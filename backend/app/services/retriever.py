"""检索服务（M3 问答用）：把用户问题转成向量 → 在 Qdrant 中找最相关的资料片段。

流程：问题 → 百炼向量化 → Qdrant 语义检索候选 → 文档级去噪 → 返回命中片段列表。
设计要点：
- 命中片段是"引用"的数据源，前端回答的 [1][2] 角标即这里的片段序号；
- **多召回 + 文档级去噪**：先用宽松阈值多取回候选（retrieval_candidate_k 条），再按
  "每篇文档的最高分"做相对过滤——某篇文档的最高分远低于全场最高分文档时，说明它与
  问题主题无关（如问净化器却召回手表手册），整篇丢弃，避免无关文档混进引用；
- 同一文档内只保留与最高片段分数接近的前几条（防一篇文档不同主题的碎片凑数刷屏）；
- 相似度绝对下限（retrieval_score_threshold）在 vector_store.search 内已过滤；
- 可限定知识库范围（kb_ids），用户在前端选"某个库提问"时传该库 id。
"""

import logging

from app.core.config import settings
from app.services import vector_store
from app.services.vectorizer import embed_texts

logger = logging.getLogger("kbqa.retriever")


def _doc_level_denoise(hits: list[dict]) -> list[dict]:
    """文档级去噪：去掉"整体不相关"的文档，并压缩同一文档内的凑数片段。

    Step 1（文档级）：按 doc_id 聚合取每篇文档的最高分；最高分低于全场最高分
        ratio 倍（retrieval_margin_ratio）的文档 → 整篇丢弃。
    Step 2（片段级）：保留的文档内，按分数从高到低只留前 retrieval_chunks_per_doc 条，
        且要求分数不低于该文档最高分的 0.85 倍（防止把同文档里不同主题的碎片也带上）。
    """
    if not hits:
        return []

    # Step 1：每篇文档的最高分
    doc_best: dict = {}
    for h in hits:
        did = h.get("doc_id")
        doc_best[did] = max(doc_best.get(did, 0.0), h.get("score", 0.0))

    best_overall = max(doc_best.values(), default=0.0)
    floor = best_overall * settings.retrieval_margin_ratio

    keep_doc_ids = [did for did, s in doc_best.items() if s >= floor]
    # 保底：无论如何保留分数最高那篇文档（floor 必然 <= best，理论上 keep 非空）
    if not keep_doc_ids:
        keep_doc_ids = [max(doc_best, key=doc_best.get)]

    # Step 2：文档内压缩
    out: list[dict] = []
    for did in keep_doc_ids:
        doc_hits = sorted(
            (h for h in hits if h.get("doc_id") == did),
            key=lambda x: x.get("score", 0.0),
            reverse=True,
        )
        top_score = doc_hits[0].get("score", 0.0)
        kept = 0
        for h in doc_hits:
            if kept >= settings.retrieval_chunks_per_doc:
                break
            # 与文档最高分差距超过 15% 的片段视为另一主题，不再追加
            if h.get("score", 0.0) < top_score * 0.85:
                break
            out.append(h)
            kept += 1

    out.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    return out


async def retrieve(question: str, kb_ids: list[int] | None = None, top_k: int | None = None) -> list[dict]:
    """检索与问题最相关的资料片段（已做文档级去噪，可直接当引用用）。

    Args:
        question: 用户问题原文（直接向量化，暂不做追问改写，M4 再优化）。
        kb_ids: 限定检索的知识库 id 列表；None=检索全部已启用库。
        top_k: 最终返回的片段上限，默认用配置。

    Returns:
        命中片段列表（按相似度从高到低），每项含：
        kb_id / doc_id / doc_name / chunk_index / page / text / score。
    """
    # 1) 问题向量化
    vecs = await embed_texts([question])
    if not vecs:
        return []
    # 2) 多召回：先取回宽松的候选集（数量取配置中的较大值）
    candidate_k = max(top_k or settings.retrieval_top_k, settings.retrieval_candidate_k)
    hits = await vector_store.search(
        query_vector=vecs[0],
        kb_ids=kb_ids,
        top_k=candidate_k,
    )
    # 3) 文档级去噪
    hits = _doc_level_denoise(hits)
    # 4) 截断到最终条数
    final_k = top_k or settings.retrieval_top_k
    hits = hits[:final_k]
    logger.info(
        "检索「%s」命中 %d 个片段（限定库=%s）",
        question[:30], len(hits), kb_ids or "全部",
    )
    return hits
