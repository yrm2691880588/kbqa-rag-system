"""重排服务：在向量检索之后，再用一个小模型"精挑"一遍资料。

为什么要重排（大白话，这是个很实际的差别）：
    向量检索很快，但它是"粗筛"——把问题和资料都变成一串数字、比谁离得近。
    数字离得近不代表真的能回答这个问题。典型翻车场景：
      问："净化器的滤芯多久换一次？"
      粗筛回来的第一条可能是"净化器开箱安装说明"——
      因为这两段话里"净化器""安装""说明书编号"这些词都出现了，数字上很接近。
      但它其实答不了"多久换一次"。
    重排模型是专门干这件事的：它把"问题 + 每一条候选资料"成对地读一遍，
    直接判断"这条能不能回答"，然后重新排序。准得多，代价是多一次外部调用（有少量费用）。

三条设计原则：
    ① **配了才用**：没填 key/model 就自动跳过（不额外加开关项，少一处"以为关了其实开着"）。
    ② **失败就跳过，绝不让问答挂掉**：重排服务超时、欠费、返回垃圾，
       都只记一条日志，然后**用原来的顺序继续**。重排是"锦上添花"，
       不能因为它坏了就让人连问题都问不了。
    ③ **顺序必须稳定可复现**：分数相同的时候按原下标排，不依赖服务商返回顺序 ——
       否则同一个问题两次跑出来引用顺序不一样，排查问题时会以为见鬼了。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.core.config import settings

logger = logging.getLogger("kbqa.rerank")


@dataclass
class RerankOutcome:
    """一次重排的结果。

    hits: 重排后的片段列表（可能为空——服务失败时返回原列表）。
    applied: 是否真的用上了重排结果。False 表示"跳过了"（没配置或调用失败）。
    detail: 给日志/排查用的一句话原因。
    """

    hits: list[dict]
    applied: bool
    detail: str = ""


def is_enabled() -> bool:
    """当前是否配置了重排（填了 key 和 model 才算）。"""
    key = (getattr(settings, "rerank_api_key", "") or "").strip()
    model = (getattr(settings, "rerank_model", "") or "").strip()
    return bool(key and model)


def _order_stably(hits: list[dict], ranked: list[tuple[int, float]]) -> list[dict]:
    """把服务商给的"下标 + 分数"还原成片段列表，并按重排结果排好序。

    两个细节（都是踩过坑才会在意的地方）：
      · 服务商返回的下标可能重复或越界（各家实现不一致），一律只认合法且没出现过的；
      · 分数相同的按下标升序 —— 保证同样输入两次跑出来的顺序完全一样，
        否则"同一个问题每次引用顺序都不同"，排查时会让人怀疑自己看错了。

    ⚠️ 关键点：这里还要给每条打上 **rerank_score**，而且用的是"按名次重新编号"的分数
    （第 1 名 1.0、第 2 名 0.99、第 3 名 0.98……）。

    为什么非得重新编号，不能直接用服务商给的分数？
        因为下游"文档级去噪"最后会按分数**再排一次序**。如果我不把重排的意志写进分数里，
        那次排序就会拿原始的向量分（旧的顺序）把重排结果整个抹掉 ——
        表现是"重排明明配好了、日志也显示重排成功，但引用顺序一点没变"，
        而且不报任何错，极难发现（本项目就真的踩了这一下，靠集成测试才抓出来）。

        重新编号的另一个好处：分数严格递减、不存在并列，
        下游那次排序就变成了"稳定不动"，重排给的顺序被原样保留。
    """
    seen: set[int] = set()
    pairs: list[tuple[int, float]] = []
    for idx, score in ranked:
        if not isinstance(idx, int) or idx < 0 or idx >= len(hits) or idx in seen:
            continue
        seen.add(idx)
        pairs.append((idx, float(score)))

    pairs.sort(key=lambda p: (-p[1], p[0]))

    # 服务商可能只返回了部分结果（例如 top_n 生效），剩下的按原顺序补在后面，
    # 免得因为重排"吃掉"了几条候选，反而让引用变少。
    order = [i for i, _ in pairs]
    order.extend(i for i in range(len(hits)) if i not in seen)

    total = len(order)
    out: list[dict] = []
    for pos, i in enumerate(order):
        h = hits[i]
        if isinstance(h, dict):
            h = dict(h)
            # 按名次给一个严格递减的分：1.0、(n-1)/n、(n-2)/n ...
            h["rerank_score"] = (total - pos) / total
        out.append(h)
    return out


class DashScopeReranker:
    """阿里云百炼（DashScope）的通用重排接口。

    为什么选它：本项目的向量化用的就是百炼（text-embedding-v4），
    用同一家能少签一份合同、少管一个 key、少一套账单。
    """

    def __init__(self) -> None:
        self.api_key = (getattr(settings, "rerank_api_key", "") or "").strip()
        self.model = (getattr(settings, "rerank_model", "") or "").strip()
        self.url = (getattr(settings, "rerank_base_url", "") or "").strip()
        self.timeout = float(getattr(settings, "rerank_timeout_seconds", 10.0) or 10.0)

    async def rerank(self, query: str, hits: list[dict], top_n: int) -> RerankOutcome:
        documents = [str(h.get("text", ""))[:4000] for h in hits]
        payload = {
            "model": self.model,
            "input": {"query": query, "documents": documents},
            # return_documents=False：我们手里已经有原文了，让服务商别再回传一遍
            # （省流量，也避免两边文本不一致时对不上号）
            "parameters": {"return_documents": False, "top_n": max(1, top_n)},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return RerankOutcome(hits, False, f"重排请求失败（{exc.__class__.__name__}），已按原顺序继续")

        if not (200 <= resp.status_code < 300):
            # 只记状态码，不把响应体整段打进日志（可能含服务商回显的 key）
            return RerankOutcome(hits, False, f"重排服务返回 {resp.status_code}，已按原顺序继续")

        try:
            data = resp.json()
        except ValueError:
            return RerankOutcome(hits, False, "重排返回非 JSON，已按原顺序继续")

        results = (data.get("output") or {}).get("results")
        if not isinstance(results, list) or not results:
            return RerankOutcome(hits, False, "重排返回结果为空，已按原顺序继续")

        ranked: list[tuple[int, float]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            score = item.get("relevance_score", item.get("score", 0.0))
            try:
                ranked.append((int(idx), float(score)))
            except (TypeError, ValueError):
                continue  # 单条格式不对就跳过，不因为一条脏数据丢掉整批结果

        if not ranked:
            return RerankOutcome(hits, False, "重排结果无法解析，已按原顺序继续")

        return RerankOutcome(_order_stably(hits, ranked), True, f"已重排 {len(ranked)} 条")


def get_reranker() -> DashScopeReranker | None:
    """取重排器；没配置就返回 None（调用方据此直接跳过）。

    返回 None 而不是一个"什么都不做"的假实现，是为了让调用方**显式**分支：
    看到 `if reranker is None` 就知道这里会跳过；如果返回空实现，
    读代码的人会以为"永远都会重排"，将来排查"为什么没重排"时白费功夫。
    """
    if not is_enabled():
        return None
    return DashScopeReranker()


async def rerank_hits(query: str, hits: list[dict], top_n: int) -> RerankOutcome:
    """对候选片段做重排；任何异常都退化成"原顺序"。"""
    if not hits:
        return RerankOutcome(hits, False, "无候选片段")

    reranker = get_reranker()
    if reranker is None:
        return RerankOutcome(hits, False, "未配置重排模型，已跳过")

    try:
        outcome = await reranker.rerank(query, hits, top_n)
    except Exception:
        # 兜底：连我们自己没预料到的异常也接住。
        # 理由和上面一样 —— 重排坏掉不能让用户连问题都问不了。
        logger.exception("重排出现未预期异常，已按原顺序继续")
        return RerankOutcome(hits, False, "重排异常，已按原顺序继续")

    if not outcome.applied:
        logger.warning("重排跳过：%s", outcome.detail)
    return outcome
