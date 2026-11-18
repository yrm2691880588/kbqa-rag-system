"""双层评估：同时测「检索对不对」和「回答对不对」。

## 为什么需要两层（真实教训）

之前只测检索层，结果是：

    检索层：4/5 通过（文档都找对了）
    回答层：3/5 通过（模型拿着正确资料却说"没有"）

**检索准 ≠ 回答准。** 只看第一层会误判"系统没问题"。

而"某个指标"那个 case 尤其典型：
检索把含 `2.12%` 的片段送进提示词了，模型还是答"资料里没有"。
只测检索层的话，这个 case 会显示为通过，问题被完全掩盖。

## 两层各测什么

### 检索层（Retrieval）
    问题 → retrieve() → 有没有命中期望的那篇文档？
    指标：Hit@1 / Hit@3 / MRR

### 回答层（Answer）
    问题 → 完整 RAG 链路 → 回答里有没有期望的关键词？
    指标：关键词命中率

关键词匹配是**刻意选择的简单做法**：
  · 不用模型判分 —— 那会引入不确定性，还可能自己判错
  · 关键词是客观的：答对了就一定有那个数字/词
  · 代价是必须人工写好期望关键词

## 评估集格式

    {
      "cases": [
        {
          "question": "某公司2025年利润是多少",
          "expect_docs": ["01_某公司2025年度报告.md"],
          "expect_keywords": ["142.7"],
          "note": "可选，写这条为什么重要"
        }
      ]
    }

`expect_keywords` 为空时只测检索层，跳过回答层。
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

import argparse
import asyncio
import json
import pathlib
import time

BACKEND = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.services.eval_metrics import (  # noqa: E402
    CaseResult,
    compare,
    evaluate_case,
    summarize,
)
from app.services.rag import stream_answer  # noqa: E402
from app.services.retriever import retrieve  # noqa: E402

DEFAULT_SET = BACKEND / "eval" / "eval_set.json"


def load_cases(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        print(f"[错误] 找不到评估集：{path}")
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[错误] 评估集不是合法 JSON：{e}")
        return []
    cases = data.get("cases") or []
    if not cases:
        print("[错误] 评估集里没有 cases")
        return []
    return cases


async def resolve_kb_ids(names: list[str]) -> list[int]:
    """把评估集里写的知识库名换成 id。"""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.name.in_(names),
                )
            )
        ).scalars().all()
    return [r.id for r in rows]


async def run_answer_layer(
    question: str,
    kb_ids: list[int] | None,
) -> str:
    """跑完整的问答链路，返回最终答案文本。"""
    answer_parts: list[str] = []
    try:
        async for event, data in stream_answer(
            question, [], kb_ids, cache_scope="", client=None
        ):
            if event == "answer":
                answer_parts.append(data.get("text", ""))
            elif event == "replace":
                # 自检重做后的完整答案：**整体替换**之前的内容
                answer_parts = [data.get("text", "")]
            elif event == "no_hit":
                answer_parts.append(data.get("message", ""))
    except Exception as exc:  # noqa: BLE001
        return f"[异常] {type(exc).__name__}: {exc}"
    return "".join(answer_parts).strip()


def keyword_hit(answer: str, keywords: list[str]) -> tuple[bool, list[str]]:
    """检查答案里是否包含期望关键词。

    Returns:
        (是否全部命中, 未命中的关键词列表)
    """
    if not keywords:
        return True, []
    missing = [k for k in keywords if k not in answer]
    return (not missing), missing


async def main() -> int:
    ap = argparse.ArgumentParser(description="双层评估：检索 + 回答")
    ap.add_argument("--set", default=str(DEFAULT_SET), help="评估集 JSON 路径")
    ap.add_argument("--top-k", type=int, default=5, help="检索看前几条")
    ap.add_argument("--kb", default="", help="知识库名（多个用逗号分隔），默认全部")
    ap.add_argument("--skip-answer", action="store_true", help="只测检索层（省钱省时间）")
    ap.add_argument("--json", default="", help="把结果另存为 JSON，便于历史对比")
    args = ap.parse_args()

    cases = load_cases(pathlib.Path(args.set))
    if not cases:
        return 1

    kb_ids: list[int] | None = None
    if args.kb:
        names = [x.strip() for x in args.kb.split(",") if x.strip()]
        kb_ids = await resolve_kb_ids(names)
        if not kb_ids:
            print(f"[警告] 找不到知识库：{names}，将检索全部")
            kb_ids = None
        else:
            print(f"限定知识库 id：{kb_ids}")

    print("=" * 72)
    print(f"双层评估：{len(cases)} 个问题")
    # 重排是否启用：看有没有配 model + key（没有 rerank_enabled 这个开关）
    rerank_on = bool(settings.rerank_model and settings.rerank_api_key)
    print(f"配置：top_k={settings.retrieval_top_k}  "
          f"max_docs={settings.retrieval_max_docs}  "
          f"rerank={'开' if rerank_on else '关'}  "
          f"领域={settings.domain}")
    print("=" * 72)

    # ---------- 第一层：检索 ----------
    print("\n【第一层：检索质量】")
    results: list[CaseResult] = []
    for i, case in enumerate(cases, 1):
        q = case["question"]
        expect = case.get("expect_docs") or []
        try:
            hits = await retrieve(q, kb_ids=kb_ids, top_k=args.top_k)
            ranked = [h.get("doc_name", "") for h in hits]
        except Exception as exc:  # noqa: BLE001
            ranked = []
            res = CaseResult(question=q, ranked_docs=[], error=str(exc))
            results.append(res)
            print(f"  [{i:2}] ERROR {q[:36]}")
            continue

        res = evaluate_case(q, ranked, expect)
        results.append(res)
        mark = "OK  " if res.hit_at_3 else "MISS"
        print(f"  [{i:2}] {mark} {q[:40]}")
        if not res.hit_at_3 and expect:
            print(f"        期望：{expect}")
            print(f"        实际：{ranked[:3]}")

    summary = summarize(results)
    print("\n检索层结果：")
    print(f"  Hit@1 = {summary.hit_at_1:.1%}")
    print(f"  Hit@3 = {summary.hit_at_3:.1%}")
    print(f"  MRR   = {summary.mrr:.3f}")

    # ---------- 第二层：回答 ----------
    answer_results = []
    if args.skip_answer:
        print("\n【第二层：回答质量】已跳过（--skip-answer）")
    else:
        print("\n【第二层：回答质量】")
        print("  （这一层会真实调用大模型，比检索层慢很多）")
        t0 = time.time()
        for i, case in enumerate(cases, 1):
            kws = case.get("expect_keywords") or []
            if not kws:
                continue
            q = case["question"]
            answer = await run_answer_layer(q, kb_ids)
            ok, missing = keyword_hit(answer, kws)
            answer_results.append({
                "question": q,
                "ok": ok,
                "missing": missing,
                "answer": answer[:300],
            })
            mark = "OK  " if ok else "FAIL"
            print(f"  [{i:2}] {mark} {q[:40]}")
            if not ok:
                print(f"        期望包含：{kws}")
                print(f"        缺失：{missing}")
                print(f"        回答：{answer[:150]}")
        elapsed = time.time() - t0

        if answer_results:
            passed = sum(1 for r in answer_results if r["ok"])
            total = len(answer_results)
            print(f"\n回答层结果：{passed}/{total} 通过（{passed / total:.1%}）")
            print(f"  耗时 {elapsed:.1f} 秒")

    # ---------- 汇总 ----------
    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    print(f"  检索 Hit@3：{summary.hit_at_3:.1%}")
    if answer_results:
        passed = sum(1 for r in answer_results if r["ok"])
        total = len(answer_results)
        print(f"  回答通过率：{passed}/{total}（{passed / total:.1%}）")

        # 关键：检索对但回答错的 case —— 这是最值得看的
        retrieval_ok = {r.question for r in results if r.hit_at_3}
        answer_bad = [r for r in answer_results if not r["ok"]]
        gap = [r for r in answer_bad if r["question"] in retrieval_ok]
        if gap:
            print(f"\n  ⚠️ 有 {len(gap)} 个「检索到了但答错」的 case（最该关注）：")
            for r in gap:
                print(f"     · {r['question'][:50]}")
                print(f"       缺：{r['missing']}")

    if args.json:
        out = pathlib.Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "domain": settings.domain,
            "retrieval": {
                "hit_at_1": summary.hit_at_1,
                "hit_at_3": summary.hit_at_3,
                "mrr": summary.mrr,
            },
            "answer": {
                "total": len(answer_results),
                "passed": sum(1 for r in answer_results if r["ok"]),
                "details": answer_results,
            },
            "failures": [
                {"question": r.question, "ranked": r.ranked_docs[:3],
                 "expect": r.expect_docs}
                for r in results if not r.hit_at_3
            ],
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已保存：{out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
