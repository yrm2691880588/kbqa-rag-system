"""Qdrant 向量库封装：集合管理 / 批量写入 / 按文档删除 / 检索。

设计约定：
- 所有知识库共用一个集合（同一 embedding 模型、同维度），payload 带 kb_id/doc_id 过滤；
- 向量点 ID 用 uuid5(NAMESPACE_DNS, "doc-{doc_id}-chunk-{i}") 生成确定性 UUID
  （Qdrant 只接受整数或 UUID，不能用 "d12-c3" 这种字符串）。
  同一文档重传时先按 doc_id 删除旧点再写入，保证幂等；
- kb_id / doc_id 建了 payload 索引，过滤删除/检索都很快。
"""

import logging
import uuid

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchText,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    TextIndexParams,
    TokenizerType,
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

    # 全文索引：支撑关键词检索（混合检索的另一路）。
    #
    # 为什么用 multilingual tokenizer：
    #   中文没有空格，默认的 word tokenizer 会把整句当一个 token，
    #   等于完全搜不到。multilingual 能正确切分中文。
    #   实测：查「某个指标」「另一个指标」都能命中。
    #
    # 为什么 min_token_len=1：
    #   财务场景常有单字有意义的情况（如「元」），
    #   设成 2 会漏掉。代价是索引稍大，可接受。
    try:
        await client.create_payload_index(
            settings.qdrant_collection,
            "text",
            TextIndexParams(
                type="text",
                tokenizer=TokenizerType.MULTILINGUAL,
                min_token_len=1,
                max_token_len=30,
                lowercase=True,
            ),
        )
    except Exception:
        pass  # 已存在


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


async def delete_document_chunks(doc_id: int) -> None:
    """按 doc_id 删除某文档的全部向量点（删文档 / 重传覆盖前调用）。

    关于返回值：
        早期这里标注 `-> int`、docstring 写"返回删除点数"，
        但实际返回的是 Qdrant 的 `res.status`（枚举/字符串），
        类型与文档都对不上。

        经核实：所有调用方（api/v1/documents.py、services/ingestion.py）
        都只关心"删掉了"，**不使用返回值**；而 Qdrant 的 delete 接口
        本身也不返回精确删除条数（只有操作状态）。
        所以这里如实改成 `-> None`，避免误导后续调用方去用这个值。
    """
    client = get_client()
    await client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=Filter(
            must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
        ),
    )


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


# 中文疑问句里常见的"无信息"成分。
# 去掉它们之后剩下的才是真正的检索意图。
_QUESTION_STOPWORDS = (
    "是多少", "是什么", "是哪些", "怎么样", "怎么办", "为什么",
    "请问", "请", "帮我", "帮忙", "告诉我",
    "什么", "哪些", "哪个", "哪家", "哪位", "多少", "几个", "怎样", "怎么",
    "如何", "是否", "有没有", "能不能", "可不可以", "可以", "需要",
    "吗", "呢", "吧", "啊", "呀", "的", "了", "是", "有",
    # 单字介词/连词：切 n-gram 时要当作分隔符，
    # 否则会切出「委会对渠」这种碎片
    "对", "在", "和", "与", "及", "或", "把", "被", "给", "从", "向", "为",
    # 常见动词/形式词：它们不是检索对象，切 n-gram 时要断开
    "面临", "存在", "属于", "包括", "包含", "涉及", "关于", "对于", "如何",
)

# 分隔符：把问句切成若干候选词
_SPLIT_CHARS = "，。？、；：！,.;:?! \t" + chr(10)


def extract_keywords(question: str, max_kw: int = 8) -> list[str]:
    """从问句里提取可用于全文检索的关键词。

    ## 为什么需要这一步

        Qdrant 的 MatchText 要求"完整包含传入的字符串"。
        直接传整个问句（「某公司的某个指标是多少？」）匹配不到 ——
        文档里只有「某个指标」，没有那句完整的话。

        实测：
          keyword_search("某公司的某个指标是多少？") → 0 条
          keyword_search("某个指标")                  → 7 条

    ## 提取策略

        1. 按标点/空白切成片段
        2. 去掉疑问停用词
        3. **对长片段切 n-gram**（这一步是关键）
        4. 过滤长度 < 2 的碎片
        5. 去重，限制总数

    ## 为什么必须切 n-gram

        实测：「投委会对渠道扩张是怎么决定的？」
        去掉停用词后得到「投委会对渠道扩张」这 8 个字，
        但文档里写的是「渠道扩张方案」——
        「投委会对渠道扩张」不是任何文档的子串，匹配不到。

        切 2~4 字子串后，能得到「渠道扩张」，
        这才是文档里真实存在的词。

    ## 为什么只切 2~4 字

        中文实词以 2~4 字为主（如"指标A"3字、"某个长指标"5字）。
        切 1 字会产生大量噪音（"的""是"），切 5 字以上命中率低。

    Args:
        question: 用户问题原文。
        max_kw: 最多返回几个关键词。

    Returns:
        关键词列表（可能为空）。
    """
    q = (question or "").strip()
    if not q:
        return []

    # ---- 1) 按分隔符切片段 ----
    parts: list[str] = []
    buf = ""
    for ch in q:
        if ch in _SPLIT_CHARS:
            if buf:
                parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        parts.append(buf)

    # ---- 2) 去停用词 ----
    cleaned: list[str] = []
    for part in parts:
        p2 = part
        for sw in _QUESTION_STOPWORDS:
            p2 = p2.replace(sw, " ")
        for piece in p2.split():
            piece = piece.strip()
            if len(piece) >= 2:
                cleaned.append(piece)

    # ---- 3) 长片段切 n-gram（关键步骤）----
    #
    # ⚠️ 只切"实词区间"，不切整个长句。
    #
    #   第一版对整个长片段暴力切 2~4 字，
    #   结果「投委会对渠道扩张」切出了
    #   ['委会对渠', '会对渠道', '对渠道扩'] 这类无意义碎片 ——
    #   它们既不在文档里，又白白占用检索名额。
    #
    #   现在改成：先找出片段里"看起来是实词"的连续区间，只对区间切。
    #   实词区间的判定：不含停用词的最长连续子串。
    expanded: list[str] = []
    for piece in cleaned:
        if len(piece) <= 4:
            expanded.append(piece)
            continue

        # 保留原片段（万一整段能命中）
        expanded.append(piece)

        # 找出"实词区间"：不含任何停用词的连续段
        segments: list[str] = []
        seg = ""
        i = 0
        while i < len(piece):
            # 检查从 i 开始是否匹配某个停用词
            matched = None
            for sw in _QUESTION_STOPWORDS:
                if sw and piece.startswith(sw, i):
                    matched = sw
                    break
            if matched:
                if seg:
                    segments.append(seg)
                seg = ""
                i += len(matched)
            else:
                seg += piece[i]
                i += 1
        if seg:
            segments.append(seg)

        # 只对实词区间切 n-gram
        for s in segments:
            s = s.strip()
            if len(s) < 2:
                continue
            if len(s) <= 4:
                expanded.append(s)
                continue
            for n in (4, 3, 2):
                for j in range(len(s) - n + 1):
                    expanded.append(s[j : j + n])

    # ---- 4) 去重保序 + 限制数量 ----
    # 长的优先（长的更具体，命中更准）
    seen: set = set()
    out: list[str] = []
    for kw in sorted(expanded, key=len, reverse=True):
        if kw in seen:
            continue
        seen.add(kw)
        out.append(kw)
        if len(out) >= max_kw:
            break

    return out

async def keyword_search(
    query: str,
    kb_ids: list[int] | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """关键词检索：按字面命中找片段（混合检索的另一路）。

    ## 与向量检索的分工

        向量检索：语义相近（"收入"能匹配"营业额"）
        关键词检索：字面精确（"2.12"只匹配含 2.12 的片段）

        两者互补。向量找"意思对"的，关键词找"字面有"的。

    ## 为什么必须用索引过滤，不能 scroll 后置过滤

        第一版写成"scroll 拿一批 → Python 判断包含"，
        结果查「某个指标」返回 0 条 —— 因为 scroll 拿的是
        **集合里前 N 条**，不是"匹配查询的前 N 条"。
        后置过滤等于在小样本里碰运气。

        现在用 MatchText 让数据库做筛选，返回的才是真正匹配的。

    ## 已知限制

        纯数字（如「8642」）在 Qdrant 全文索引里匹配不到 ——
        multilingual tokenizer 对纯数字的处理不同。
        所以这一路只是**补充**，不能替代向量检索。

    Args:
        query: 用户问题原文。
        kb_ids: 限定知识库；None=不限制。
        top_k: 返回条数上限。

    Returns:
        命中列表，格式与 `search()` 一致。
        出错时返回空列表 —— 关键词检索失败不应该让整个问答挂掉。
    """
    q = (query or "").strip()
    if not q:
        return []

    client = get_client()
    limit = int(top_k or settings.retrieval_top_k)

    # 过滤条件：领域/组织范围（kb_ids）+ 全文匹配
    must: list = []

    if kb_ids is not None:
        if not kb_ids:
            return []
        if len(kb_ids) == 1:
            must.append(FieldCondition(key="kb_id", match=MatchValue(value=kb_ids[0])))
        else:
            must.append(FieldCondition(key="kb_id", match=MatchAny(any=kb_ids)))

    # ⚠️ 不能用整个问句去匹配。
    #
    #   MatchText 要求"完整包含传入的字符串"，而问句是
    #   「某公司的某个指标是多少？」，文档里只有「某个指标」——
    #   前者不是后者的子串，匹配不到（实测 0 条）。
    #
    #   所以先提取关键词，逐个匹配后合并。
    kws = extract_keywords(q)
    if not kws:
        # 提不出关键词（比如纯符号）→ 退回整句匹配，给一次机会
        kws = [q]

    seen_ids: set = set()
    hits: list = []
    per_kw = max(3, limit // max(1, len(kws)))   # 每个词分到的名额

    for kw in kws:
        conds = list(must)
        conds.append(FieldCondition(key="text", match=MatchText(text=kw)))
        try:
            batch, _ = await client.scroll(
                collection_name=settings.qdrant_collection,
                scroll_filter=Filter(must=conds),
                limit=per_kw,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("关键词「%s」检索失败：%s", kw, e)
            continue

        for h in batch:
            if h.id in seen_ids:
                continue
            seen_ids.add(h.id)
            hits.append(h)

    if not hits:
        return []

    out: list[dict] = []
    for h in hits:
        payload = h.payload or {}
        text = str(payload.get("text") or "")
        if not text:
            continue
        out.append({
            "id": str(h.id),
            # 关键词命中的分数固定为 1.0：
            # RRF 只用名次，不看绝对值，所以这里给什么都行。
            "score": 1.0,
            "text": text,
            "doc_id": payload.get("doc_id"),
            "doc_name": payload.get("doc_name"),
            "page": payload.get("page"),
            "kb_id": payload.get("kb_id"),
            "chunk_index": payload.get("chunk_index"),
            "_keyword_hit": True,
        })

    return out

async def search(
    query_vector: list[float],
    kb_ids: list[int] | None = None,
    top_k: int | None = None,
    score_threshold: float | None = None,
) -> list[dict]:
    """语义检索：在向量库中找与 query_vector 最相似的片段。

    Args:
        query_vector: 问题向量化后的向量。
        kb_ids: 限定检索的知识库 id 列表；None=不限制（仅平台级调用才会这么传）。
            ⚠️ 注意语义区分（多租户安全关键）：
              - None  → 不加过滤，检索所有库；
              - []    → 明确表示"该用户一个库都不能用"，直接返回空结果。
            两者绝不能混为一谈。历史上这里写的是 `if kb_ids:`，空列表会被当成
            "没有限制"，等于把别家客户的知识库也搜出来了 —— 属于数据泄露，已修正。
        top_k: 取回候选片段数，默认用配置 retrieval_top_k。
        score_threshold: 相似度下限，低于该值的片段被丢弃；默认用配置。

    Returns:
        按相似度从高到低排列的命中列表，每项含：
        kb_id / doc_id / doc_name / chunk_index / page / text / score。
    """
    # 空列表 = 明确无权访问任何知识库：直接返回空，绝不能退化成"不加过滤"
    if kb_ids is not None and len(kb_ids) == 0:
        return []

    client = get_client()
    top_k = top_k or settings.retrieval_top_k
    threshold = settings.retrieval_score_threshold if score_threshold is None else score_threshold

    must: list = []
    # 注意用 `is not None` 而不是真值判断，避免空列表被误当成"不限制"
    if kb_ids is not None:
        # 单库用 MatchValue（精确等于），多库必须用 MatchAny（属于集合）。
        #
        # ⚠️ 这里曾经写错过：`MatchValue(value=kb_ids[0] if len==1 else kb_ids)`
        #    想把列表塞进 MatchValue，但 MatchValue.value 的类型是
        #    Union[StrictBool, StrictInt, StrictStr] —— 只接受**单个值**。
        #    传 [1,2] 会抛 pydantic ValidationError（Input should be a valid boolean）。
        #    后果：一个会话限定 ≥2 个知识库时检索直接崩。
        #    网页端因为 ChatView 只传单库而侥幸没暴露，
        #    但 /open/chat 传的是组织内全部库，必然触发。
        match = (
            MatchValue(value=kb_ids[0])
            if len(kb_ids) == 1
            else MatchAny(any=list(kb_ids))
        )
        must.append(FieldCondition(key="kb_id", match=match))
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
