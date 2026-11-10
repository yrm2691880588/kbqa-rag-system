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
from app.services import rerank as rerank_service
from app.services import vector_store
from app.services.vectorizer import embed_texts

logger = logging.getLogger("kbqa.retriever")


def _score_of(hit: dict) -> float:
    """取一条片段"用于排序"的分数。

    ⚠️ 这里为什么不能直接用 hit["score"]：
        score 是**向量相似度**（粗筛的分数）；如果这条片段经过重排，
        重排会把它的名次写在 rerank_score 里。下游排序必须认 rerank_score，
        否则会把重排辛苦调好的顺序又按旧的向量分排回去 ——
        表现是"配了重排但顺序没变、也不报错"（本项目真实踩过，靠集成测试抓出来的）。
    """
    if "rerank_score" in hit:
        return float(hit.get("rerank_score") or 0.0)
    return float(hit.get("score", 0.0) or 0.0)


def _doc_level_denoise(hits: list[dict]) -> list[dict]:
    """文档级去噪：去掉"整体不相关"的文档，并压缩同一文档内的凑数片段。

    Step 1（文档级）：按 doc_id 聚合取每篇文档的最高分；最高分低于全场最高分
        ratio 倍（retrieval_margin_ratio）的文档 → 整篇丢弃。
        （注意：开了重排时"分数"用的是重排名次分，那时这一步基本不会误砍，
          因为重排自己已经做过相关性判断了；真正生效的是下面的文档数上限。）
    Step 1.5（文档数上限）：留下的文档按最高分排序，最多保留 retrieval_max_docs 篇。
        这一步是"防引用列表变成长杂货单"：宽泛问题可能同时满足好几篇文档，
        全带上会让回答又长又散，还多花 token。
    Step 2（片段级）：保留的文档内，按分数从高到低只留前 retrieval_chunks_per_doc 条，
        且要求分数不低于该文档最高分的 0.85 倍（防止把同文档里不同主题的碎片也带上）。
    """
    if not hits:
        return []

    # Step 1：每篇文档的最高分
    doc_best: dict = {}
    for h in hits:
        did = h.get("doc_id")
        doc_best[did] = max(doc_best.get(did, 0.0), _score_of(h))

    best_overall = max(doc_best.values(), default=0.0)
    floor = best_overall * settings.retrieval_margin_ratio

    # 开了重排就**跳过**这道"相对比例"过滤。
    # 原因：重排给出的分数是**名次分**（第 1 名 1.0、第 2 名 0.67、第 3 名 0.33……），
    # 它天然是"拉开档次"的，拿它去和 0.72 这种比例阈值比，
    # 会把第 2、3 名这些**本来相关**的文档整篇砍掉 ——
    # 表现成"开了重排之后引用反而变少了"，越优化越差。
    # 开了重排时，"哪些文档相关"这件事交给重排判断，这里只负责数量和条数上的收敛。
    reranked = any("rerank_score" in h for h in hits)
    if reranked:
        keep_doc_ids = list(doc_best.keys())
    else:
        keep_doc_ids = [did for did, s in doc_best.items() if s >= floor]
        # 保底：无论如何保留分数最高那篇文档（floor 必然 <= best，理论上 keep 非空）
        if not keep_doc_ids:
            keep_doc_ids = [max(doc_best, key=doc_best.get)]

    # Step 1.5：限制"最多有几个不同文档参与引用"（retrieval_max_docs）。
    # 为什么需要：上面只按"相对最高分的比例"筛，如果问的是一句很宽泛的话，
    # 可能同时满足条件的文档有五六个，引用列表就会变得又长又杂（用户看不完，
    # 而且每多一篇就多一段没用的上下文塞给模型，白花 token）。
    # 按每篇文档的最高分排序，只留前 N 篇 —— 分数最高的那篇一定在，不会被误砍。
    max_docs = int(getattr(settings, "retrieval_max_docs", 0) or 0)
    if max_docs > 0 and len(keep_doc_ids) > max_docs:
        keep_doc_ids = sorted(keep_doc_ids, key=lambda d: doc_best[d], reverse=True)[:max_docs]

    # Step 2：文档内压缩
    out: list[dict] = []
    for did in keep_doc_ids:
        doc_hits = sorted(
            (h for h in hits if h.get("doc_id") == did),
            key=_score_of,
            reverse=True,
        )
        top_score = _score_of(doc_hits[0])
        kept = 0
        for h in doc_hits:
            if kept >= settings.retrieval_chunks_per_doc:
                break
            # 与文档最高分差距超过 15% 的片段视为另一主题，不再追加
            if _score_of(h) < top_score * 0.85:
                break
            out.append(h)
            kept += 1

    out.sort(key=_score_of, reverse=True)
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
    # 2.5) 重排（P2-21）：把粗筛出来的候选交给重排模型精挑一遍。
    #      注意顺序 —— 重排要放在"文档级去噪"**之前**：
    #      去噪是靠"分数之间的相对比例"来判断哪篇文档不相关的，
    #      而重排恰好把"只是词面像、其实答不了问题"的片段分数压下去，
    #      先去噪再重排就浪费了重排最擅长的那部分判断。
    #      没配置重排模型时会直接跳过；重排服务挂了也会退化成原顺序（不影响问答）。
    if hits:
        outcome = await rerank_service.rerank_hits(
            question, hits, top_n=max(top_k or settings.retrieval_top_k, settings.retrieval_candidate_k)
        )
        hits = outcome.hits
        if outcome.applied:
            logger.info("检索「%s」%s", question[:30], outcome.detail)
    # 3) 文档级去噪
    hits = _doc_level_denoise(hits)
    # 4) 截断到最终条数
    final_k = top_k or settings.retrieval_top_k
    hits = hits[:final_k]
    logger.info(
        "检索「%s」命中 %d 个片段（限定库=%s）",
        question[:30], len(hits),
        # 注意用 is not None 判断：空列表([])表示"无权访问任何库"，
        # 早期这里写的是 `kb_ids or "全部"`，空列表会被显示成"全部"，排查问题时会被误导
        "全部" if kb_ids is None else f"{len(kb_ids)} 个",
    )
    return hits
