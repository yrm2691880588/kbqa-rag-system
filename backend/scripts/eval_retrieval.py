"""检索质量评估脚本（P2-21）：把"检索到底准不准"变成一组能对比的数字。

怎么用（backend 目录下）：

    # 1) 先复制评估集模板，按自己的资料改成真实问题
    copy eval\\retrieval_eval_set.example.json eval\\retrieval_eval_set.json

    # 2) 用当前配置跑一遍（会自动带上重排，如果你配了的话）
    python scripts\\eval_retrieval.py

    # 3) 关掉重排再跑一遍，看看重排到底有没有用（这一步才是关键）
    python scripts\\eval_retrieval.py --no-rerank

    # 4) 两份结果对比着看，就能回答"开重排值不值那点钱"

前置条件：PostgreSQL / Qdrant 已启动，且评估集里提到的资料**已经上传**到知识库。
    没上传的资料当然检索不到，会被算成"未命中"——这不是 bug。

为什么要有这个脚本（大白话）：
    调检索参数最容易犯的错是"凭感觉"。改完觉得好像好了，其实可能更差了。
    有了这个脚本，改完跑一遍，直接看命中率的百分比变化。

输出说明：
    · 第一条即命中 —— 最严格，用户点开就答对
    · 前 3 条命中 —— 常见的可用标准（引用列表一般 3 条左右）
    · MRR        —— 答案排得越靠前分越高
    · 未命中清单 —— 哪些问题没找到，最值得逐条看
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402

from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.services import rerank as rerank_service  # noqa: E402
from app.services.eval_metrics import CaseResult, compare, evaluate_case, summarize  # noqa: E402
from app.services.retriever import retrieve  # noqa: E402

DEFAULT_SET = BACKEND / "eval" / "retrieval_eval_set.json"
EXAMPLE_SET = BACKEND / "eval" / "retrieval_eval_set.example.json"


def load_cases(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        print(f"❌ 找不到评估集：{path}")
        print(f"   可以先复制模板：copy \"{EXAMPLE_SET.name}\" \"{path.name}\"（在同目录下）")
        sys.exit(2)
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases") or []
    if not cases:
        print("❌ 评估集里没有 cases")
        sys.exit(2)
    return cases


async def resolve_kb_ids(names: list[str]) -> list[int] | None:
    """把评估集里写的知识库名转成 id。写不出来就返回 None（= 查全部库）。"""
    if not names:
        return None
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(select(KnowledgeBase.id, KnowledgeBase.name))
        ).all()
    wanted = {n.strip().lower() for n in names}
    ids = [int(r[0]) for r in rows if str(r[1]).strip().lower() in wanted]
    missing = wanted - {str(r[1]).strip().lower() for r in rows}
    if missing:
        print(f"   ⚠️ 评估集里提到的知识库不存在，已忽略：{'、'.join(sorted(missing))}")
    return ids or None


async def run_one(case: dict, kb_ids: list[int] | None, top_k: int) -> CaseResult:
    question = str(case.get("question", "")).strip()
    expect = case.get("expect_docs") or []
    try:
        hits = await retrieve(question, kb_ids=kb_ids, top_k=top_k)
    except Exception as exc:
        # 检索本身失败也算一条（计入分母），不然"把出错的问题剔掉"会让成绩虚高
        return evaluate_case(question, [], expect, error=f"{type(exc).__name__}: {str(exc)[:120]}")
    ranked = [str(h.get("doc_name", "")) for h in hits]
    return evaluate_case(question, ranked, expect)


async def main() -> int:
    parser = argparse.ArgumentParser(description="检索质量评估（P2-21）")
    parser.add_argument("--set", default=str(DEFAULT_SET), help="评估集 JSON 路径")
    parser.add_argument("--top-k", type=int, default=5, help="每条问题看前几条结果（默认 5）")
    parser.add_argument("--no-rerank", action="store_true",
                        help="临时关掉重排，用来对比'开重排到底有没有用'")
    parser.add_argument("--json", default="", help="把结果另存为 JSON（便于和历史对比）")
    args = parser.parse_args()

    # 关掉重排的办法：把 key 和 model 临时清空（get_reranker 见到空值就会跳过）。
    # 这样做而不是加一个开关参数，是为了**走的是同一条代码路径** ——
    # 用别的近路绕过重排，很可能绕过了别的东西，对比就不公平了。
    if args.no_rerank:
        from app.core.config import settings
        settings.rerank_api_key = ""
        settings.rerank_model = ""

    cases = load_cases(pathlib.Path(args.set))
    print("=" * 74)
    print("检索质量评估")
    print("=" * 74)
    print(f"评估集：{args.set}（{len(cases)} 个问题）")
    print(f"重排  ：{'已关闭（对比用）' if args.no_rerank else ('已启用 ' + str(rerank_service.is_enabled()))}")
    print("-" * 74)

    results: list[CaseResult] = []
    for i, case in enumerate(cases, 1):
        kb_ids = await resolve_kb_ids(case.get("kb_names") or [])
        res = await run_one(case, kb_ids, args.top_k)
        results.append(res)
        mark = "✅" if res.hit_at_3 else "❌"
        rank = res.matched_rank
        where = f"第 {rank} 位" if rank else "未命中"
        print(f"  {mark} [{i:>2}/{len(cases)}] {res.question[:36]:<36} {where}")
        if res.error:
            print(f"           ⚠️ {res.error}")

    summary = summarize(results)
    print("-" * 74)
    for k, v in summary.as_row().items():
        print(f"  {k:<14}{v}")

    if summary.misses:
        print(f"\n未命中（前 {args.top_k} 条里都没有期望资料）的 {len(summary.misses)} 个问题：")
        for m in summary.misses[:15]:
            got = "、".join(m.ranked_docs[:3]) or "（没有任何结果）"
            print(f"  · {m.question}")
            print(f"      期望：{'、'.join(m.expect_docs)}")
            print(f"      实得：{got}")

    if args.json:
        out = pathlib.Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "rerank": not args.no_rerank,
            "summary": summary.as_row(),
            "cases": [
                {"question": r.question, "expect": r.expect_docs,
                 "got": r.ranked_docs, "matched_rank": r.matched_rank, "error": r.error}
                for r in results
            ],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已保存：{out}")

    print("\n提示：再跑一次 `--no-rerank` 并把两次的 JSON 对比，就能算出重排带来的实际提升。")
    return 0


if __name__ == "__main__":
    print("提示：需要 PostgreSQL / Qdrant 已启动，且评估集里的资料已上传到知识库。\n")
    sys.exit(asyncio.run(main()))
