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

# RRF 的平滑常数。原论文（Cormack et al. 2009）的经验值。
# 作用：压平"第 1 名 vs 第 2 名"的差距，让"两路都排中游"能超过"一路独苗第一"。
_RRF_K = 60

# 关键词补充时最多替换几条向量结果。
#
# 为什么设上限：补充的片段相关性未经向量验证，
# 替换太多等于"用关键词结果顶替向量结果"，那正是全量 RRF 融合失败的原因。
# 2 条是个平衡点：够把漏掉的关键片段补进来，又不会打乱向量的整体判断。
_MAX_REPLACE = 2


def rrf_fuse(
    result_lists: list[list[dict]],
    top_k: int,
) -> list[dict]:
    """把多路检索结果用 RRF（倒数排名融合）合并。

    ## 为什么不用分数相加

        两路分数不可比：
          向量检索 = 余弦相似度（0~1）
          关键词检索 = 固定 1.0（或 BM25 分数，范围不定）
        直接相加会让某一路永远占优。

    ## RRF 怎么算

        score(doc) = Σ  1 / (k + rank_i)

        只用名次，不用绝对分数。被多路同时命中的文档得分最高。

    Args:
        result_lists: 多路结果，每路是 search() 返回的那种列表。
        top_k: 融合后保留多少条。

    Returns:
        融合后的列表，按 RRF 得分降序。

    Note:
        `_rrf_score` 是额外加的字段，用于调试；
        原有的 score 字段保持不变（下游可能要用）。
    """
    if not result_lists:
        return []

    # 用 (doc_id, chunk_index) 做唯一键：
    # 同一个片段在不同路里的 id 可能不同（scroll 的 id 是 uuid），
    # 但 (doc_id, chunk_index) 是稳定的业务标识。
    scores: dict[tuple, float] = {}
    by_key: dict[tuple, dict] = {}

    for hits in result_lists:
        for rank, h in enumerate(hits, start=1):
            key = (h.get("doc_id"), h.get("chunk_index"))
            # 有些来源可能没有 chunk_index，退回用 text 前 80 字做键
            if key[1] is None:
                key = (h.get("doc_id"), (h.get("text") or "")[:80])
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            # 保留第一次见到的原始对象（后续只加分数）
            by_key.setdefault(key, h)

    fused = []
    for key, s in scores.items():
        h = dict(by_key[key])
        h["_rrf_score"] = s
        fused.append(h)

    fused.sort(key=lambda x: x["_rrf_score"], reverse=True)
    return fused[:top_k]



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

    # ── 交错排序（round-robin）──
    #
    # 为什么不能纯按分数排（实测问题）：
    #   `retrieval_chunks_per_doc` 允许同一文档贡献多条片段。
    #   当某篇文档与问题高度相关时，它的 3 条会把前 3 名全占，
    #   而答案可能只在第 2 篇文档里 —— 表现成 Hit@3 未命中，
    #   也给模型一份"全是同一文件"的材料。
    #
    #   实测三个失败 case 都是这个病根：
    #     净息差 → [xlsx, xlsx, 分季度] 年报排第 5
    #     口径   → [xlsx, 年报, 年报]   口径详解排第 4
    #     风险   → [行业报告, 行业报告, xlsx]
    #
    # 交错规则：按"这条片段在本文档内的名次"分层，
    #   第 0 层（各文档最高分）先出，然后是第 1 层，依此类推。
    #   层内按分数排序。
    #
    #   这样前 N 位一定是 N 篇不同文档（前提是 N <= 文档数），
    #   引用列表的多样性得到保证。
    by_doc: dict = {}
    for h in out:
        by_doc.setdefault(h.get("doc_id"), []).append(h)

    # 每个文档内先按分数降序（同一文档的片段按相关度排）
    for did in by_doc:
        by_doc[did].sort(key=_score_of, reverse=True)

    # 按"文档内名次"分层交错
    interleaved: list[dict] = []
    depth = 0
    while True:
        layer = []
        for did, hs in by_doc.items():
            if depth < len(hs):
                layer.append(hs[depth])
        if not layer:
            break
        # 同层内按分数降序，保证高分文档优先
        layer.sort(key=_score_of, reverse=True)
        interleaved.extend(layer)
        depth += 1

    return interleaved


async def _supplement_with_keywords(
    question: str,
    vec_hits: list[dict],
    kb_ids: list[int] | None,
    final_k: int,
) -> list[dict]:
    """用关键词检索补充向量结果漏掉的片段。

    ## 判定逻辑

        1. 提取问句实词
        2. 检查向量结果是否**完整覆盖**这些实词（用 all，不是 any）
        3. 全覆盖 → 原样返回，不干扰向量的判断
        4. 有缺失 → 查关键词路，取精确命中片段
        5. **替换**向量结果末尾的低分项，而不是"填空位"

    ## 为什么是替换而不是追加（实测踩的坑）

        第一版写成"追加到末尾，直到凑满 final_k"。
        但实测「海联银行的净息差是多少？」时：
          · 向量结果 5 条，已经占满 final_k=5
          · 关键词能命中 4 条含 `2.12` 的片段
          · `if len(out) >= final_k: break` 一进去就 break
          · **一条都补不进来**，功能形同虚设

        现在改成：名额满时，用补充片段挤掉**向量结果里排名最后**的那条。
        敢挤的理由：向量末尾那几条本来就最不相关，
        而补进来的是字面精确命中的片段。

    Args:
        question: 用户问题。
        vec_hits: 向量检索（已去噪）的结果。
        kb_ids: 知识库范围。
        final_k: 最终保留条数。

    Returns:
        可能被补充过的列表。
    """
    from app.services.vector_store import extract_keywords, keyword_search

    base = list(vec_hits or [])
    if not base:
        return base

    kws = extract_keywords(question)
    if not kws:
        # 提不出实词（纯符号/纯数字）→ 无法判断，不补充
        return base

    # 向量结果是否**完整覆盖**问句里的实词？
    #
    # ⚠️ 用 all 而不是 any（踩过坑）：
    #   原来用 any，只要命中任意一个词就算覆盖。
    #   实测「投委会对渠道扩张是怎么决定的？」：
    #     提取 ['渠道扩张', '投委会', '决定']
    #     向量结果里「决定」命中 → any 判定"已覆盖" → 跳过补充
    #     但那条片段讲的是研发预算，根本不是渠道扩张的内容。
    #
    # 为什么不怕"多补"：补充只是替换末尾的低分项，
    #   误判代价 = 多一条没用的引用；漏补代价 = 答案不在上下文里。
    #   两者不对等，所以宁可宽松地触发。
    joined = " ".join((h.get("text") or "") for h in base)
    missing = [kw for kw in kws if kw not in joined]
    if not missing:
        return base

    # ---- 保护：向量已经覆盖了"核心信息"时不做替换 ----
    #
    # ## 为什么需要这道保护（实测回归）
    #
    # 「启明科技研发中心的预算执行率是多少？」提取 8 个关键词，
    # 缺失 6 个。但其中：
    #   · 「启明科技研发中心」—— 复合词，文档里写作
    #     「公司名称:启明科技；部门:研发中心」，从来不会连成一串
    #   · 「预算执行率」—— 文档里是「2025预算(万元)」「执行率」分开写的
    #
    # 而向量结果第 2 条**已经正确命中**含 `105`（执行率）的研发中心片段。
    #
    # 但补充逻辑只按"字面缺失"判断，于是执行替换，
    # 把这条正确答案挤到末尾 → 反而变差。
    #
    # ## 判定方式
    #
    # 对每个缺失词，检查它的 2 字子串是否**都已经**出现在向量结果里。
    # 全部覆盖 = 只是"表述方式不同"，不是真的漏了 → 不做替换。
    #
    # 举例：「预算执行率」的子串有 预算/算执/执行/行率。
    #   向量结果里有「2025预算」和「执行率」→ 覆盖了 预算/执行/行率 → 视为已覆盖。
    #   这样就不会误判为"漏了"。
    def _core_covered(word: str) -> bool:
        """检查这个缺失词的核心片段是否已被向量结果覆盖。"""
        if len(word) < 2:
            return True
        subs = [word[i : i + 2] for i in range(len(word) - 1)]
        if not subs:
            return True
        # 只要大部分子串出现，就认为核心已被覆盖（不要求 100%，避免过严）
        hit = sum(1 for s in subs if s in joined)
        return hit >= max(1, len(subs) * 0.6)

    real_missing = [kw for kw in missing if not _core_covered(kw)]
    if not real_missing:
        # 所有"缺失"其实只是表述差异 → 不补充
        return base

    # 查关键词路
    kw_hits = await keyword_search(question, kb_ids=kb_ids, top_k=max(final_k * 3, 15))
    if not kw_hits:
        return base

    # 只保留含实词的片段，并**按命中实词数量降序排**。
    #
    # ⚠️ 排序是必须的（实测踩的坑）：
    #   「海联银行的净息差是多少？」提取 ['海联银行', '净息差']。
    #   关键词返回 10 条：4 条同时含两个词（含 2.12 的正确答案），
    #   6 条只含「海联银行」（财务数据表等无关片段）。
    #
    #   原来不排序、按 Qdrant 返回顺序取，结果补进来的是
    #   `| 海联银行 | 134.5 | 142.7 |` 这种只含「海联银行」的片段 ——
    #   替换做了，但换进来的还是错的东西。
    #
    #   按命中数量排序后，同时含两个词的片段排最前，正确答案就补进来了。
    #
    # 判定用 any 而非 all：一个片段命中「净息差」就够有价值了，
    # 不必要求它同时含所有实词（上面那个 all 是用来判断"向量漏没漏"的，两回事）。
    # 排序规则（两级）：
    #   第一优先：命中**缺失词**的片段（那些才是向量漏掉的信息）
    #   第二优先：命中实词总数（越多越相关）
    #
    # ⚠️ 为什么必须把"缺失词"放在第一优先（实测踩的坑）：
    #   「海联银行的净息差是多少？」→ ['海联银行', '净息差']
    #   「海联银行」在向量结果里已经有了，只缺「净息差」。
    #
    #   关键词返回 14 条，全部只命中 1 个词：
    #     · 11 条只含「海联银行」← 这些信息向量已经有了，补进来没用
    #     · 3 条含「净息差」（含 2.12 的正确答案）← 这才是要补的
    #
    #   按"命中总数"排序时这两类并列（都是 1），顺序由 Qdrant 决定 ——
    #   结果补进来的是只含「海联银行」的片段，白补。
    #
    #   把"含缺失词"作为第一优先后，含「净息差」的片段必然排前面。
    scored: list[tuple[int, int, dict]] = []
    for h in kw_hits:
        text = h.get("text") or ""
        has_missing = any(kw in text for kw in real_missing)
        hits = sum(1 for kw in kws if kw in text)
        if hits > 0:
            scored.append((1 if has_missing else 0, hits, h))

    if not scored:
        return base

    # 含缺失词优先，其次命中总数多的优先；都相同时保持原顺序
    scored.sort(key=lambda x: (-x[0], -x[1]))
    picked = [h for _, _, h in scored]

    seen = {(h.get("doc_id"), h.get("chunk_index")) for h in base}
    out = list(base)
    replaced = 0
    added = 0

    # 替换位置：从末尾往前挪。
    #
    # ⚠️ 必须记录被替换的位置（踩过的坑）：
    #   第一版每次都写 `out[-1] = h` —— 第二次替换就把第一次的结果覆盖了，
    #   等于只替换成功 1 条。看起来"替换了"，实际白做。
    # ⚠️ 向量结果少于 3 条时**只追加、不替换**（实测发现的回归）。
    #
    # 为什么：结果少说明向量已经筛得很严了，那几条都是精心挑的。
    #   此时替换掉末尾，等于把"向量认为最相关的少数几条"之一踢掉，
    #   实测导致 Hit@1 下降（「渠道」「人数」两条从第 1 位掉下去）。
    #
    #   结果少时追加到末尾即可 —— 反正还有位置，没必要替换。
    can_replace = len(out) >= 3
    replace_pos = len(out) - 1

    for h in picked:
        key = (h.get("doc_id"), h.get("chunk_index"))
        if key in seen:
            continue

        if len(out) < final_k:
            # 还有空位 → 直接追加（不影响已有排序）
            out.append(h)
            added += 1
        elif can_replace:
            # 名额满且结果充足 → 从末尾往前替换（末尾最不相关）
            if replace_pos < 0 or replaced >= _MAX_REPLACE:
                break
            out[replace_pos] = h
            replace_pos -= 1
            replaced += 1
        else:
            # 名额满但结果很少 → 宁可不补，也不破坏向量的判断
            break

        seen.add(key)

        if replaced >= _MAX_REPLACE and len(out) >= final_k:
            break

    if added or replaced:
        logger.info(
            "关键词补充：缺 %s → 追加 %d 条、替换末尾 %d 条",
            real_missing, added, replaced,
        )

    return out

async def retrieve(question: str, kb_ids: list[int] | None = None, top_k: int | None = None) -> list[dict]:
    """检索与问题最相关的资料片段（已做文档级去噪，可直接当引用用）。

    Args:
        question: 用户问题原文（直接向量化，暂不做追问改写）。
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
    vec_hits = await vector_store.search(
        query_vector=vecs[0],
        kb_ids=kb_ids,
        top_k=candidate_k,
    )

    # 2.1) 先用向量结果。关键词只在"明显漏了"时补充（见下面的 3.5 步）。
    #
    # 为什么不再做全量 RRF 融合（实测数据）：
    #   全量融合：纯向量 4/6 → 混合 3/6（净效果 -1）
    #   补充模式：纯向量 3/6 → 补充后 5/6（净效果 +2，无一个变差）
    #
    #   原因：关键词提取不准时会把大量只沾边的片段拉进来，
    #   RRF 融合后噪音因为"多路命中"压过了向量检索的精确判断。
    #   典型受害 case：「负责人」本应命中组织架构文档，融合后只剩投委会纪要。
    #
    #   补充模式不挤掉任何已有结果，只做"加法"，所以风险低得多。
    hits = vec_hits
    # 2.5) 重排：把粗筛出来的候选交给重排模型精挑一遍。
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

    # 3.5) 关键词补充（只在向量"明显漏了"时介入）
    #
    # 为什么放在去噪之后：
    #   去噪是"减法"（去掉不相关文档），补充是"加法"。
    #   先减后加，补充进来的片段不会干扰去噪的相对分数判断。
    #
    # 什么时候算"明显漏了"：
    #   向量结果里**没有任何一条**包含问句中的实词。
    #   这个判定偏宽松，但代价很低 —— 补充只是加，不挤掉已有结果。
    #
    # 实测效果（6 个 case）：纯向量 3/6 → 补充后 5/6，无一个变差。
    # 4) 截断到最终条数
    #
    # ⚠️ 截断必须在"关键词补充"**之前**（踩过的坑）：
    #   第一版是先补充再截断，结果补充进来的片段排在末尾，
    #   被 `hits[:final_k]` 一刀切掉 —— 补充逻辑白做。
    #   实测：补充后确实有含 2.12 的片段在列表第 4、5 位，
    #   但截断只留前 5 条，恰好把它们砍了。
    #
    #   所以顺序改成：向量 → 去噪 → 截断 → 补充。
    #   补充函数内部会按 final_k 控制列表长度（替换末尾而非无限追加）。
    final_k = top_k or settings.retrieval_top_k
    hits = hits[:final_k]

    # 4.5) 关键词补充（只在向量"漏了实词"时介入）
    #
    # 为什么要补：向量检索对"表格里的一行"这种短文本区分度不高。
    #   实测「海联银行的净息差是多少」时，含 `2.12%` 的片段在向量结果里
    #   排第 20 位，被其他财务指标挤下去。
    #
    # 为什么放在截断之后：截断是"取前 N 条"，补充是"替换末尾的低分项"。
    #   先截断才有确定的"末尾"可替换。
    #
    # 为什么失败不影响主流程：这一路是增强，挂了就只用向量结果。
    if settings.hybrid_search_enabled and hits:
        try:
            supplemented = await _supplement_with_keywords(
                question, hits, kb_ids, final_k=final_k
            )
            hits = supplemented
        except Exception as e:  # noqa: BLE001
            logger.warning("关键词补充失败（忽略）：%s", e)
    logger.info(
        "检索「%s」命中 %d 个片段（限定库=%s）",
        question[:30], len(hits),
        # 注意用 is not None 判断：空列表([])表示"无权访问任何库"，
        # 早期这里写的是 `kb_ids or "全部"`，空列表会被显示成"全部"，排查问题时会被误导
        "全部" if kb_ids is None else f"{len(kb_ids)} 个",
    )
    return hits
