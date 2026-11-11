"""重排服务测试。

这个文件在测什么（大白话）：
    重排 = 在向量粗筛之后，再用一个小模型把候选资料"精挑"一遍重新排序。
    这里验证四件事，每一件都对应一个真实的翻车场景：

    ① **没配置就跳过**：没填 key/model 时不偷偷去调外部服务。
    ② **服务挂了不能拖垮问答**：超时、返回 500、返回一坨乱码……
       都必须退化成"用原来的顺序继续"，绝不能让人连问题都问不了。
    ③ **结果顺序要稳定可复现**：同一个问题两次跑出来的引用顺序必须一样。
       否则排查时看到"顺序怎么又变了"，会怀疑自己看错了。
    ④ **不能把候选弄丢**：服务商只返回一部分结果时（比如 top_n 生效），
       剩下的要按原顺序补回去 —— 否则"开了重排反而引用变少了"。

用什么手段验证：本地起一个**真的 HTTP 小服务**当重排接口，真发请求、真收响应。
比 mock 掉网络调用可信（mock 掉的话，请求体字段名写错了测试照样通过）。

运行方式（backend 目录下）：
    python -m unittest tests.test_rerank -v
"""

import asyncio
import contextlib
import http.server
import json
import os
import sys
import threading
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 本机自测要绕过系统代理（这台机器给子进程设了 http_proxy，
# 不设置的话连自己起在 127.0.0.1 上的小服务都会走代理，直接连不通）
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

from app.core.config import settings
from app.services import rerank as rr

_MUTABLE = ["rerank_api_key", "rerank_model", "rerank_base_url", "rerank_timeout_seconds"]


def _run(coro):
    return asyncio.run(coro)


@contextlib.contextmanager
def temp_settings(**kwargs):
    old = {k: getattr(settings, k) for k in _MUTABLE if hasattr(settings, k)}
    try:
        for k, v in kwargs.items():
            setattr(settings, k, v)
        yield settings
    finally:
        for k, v in old.items():
            setattr(settings, k, v)


def _hits(*doc_ids: int) -> list[dict]:
    """造几条候选片段（只要 doc_id 和 text 两个字段就够重排用）。"""
    return [
        {"doc_id": d, "doc_name": f"资料{d}.pdf", "text": f"第 {d} 条内容", "score": 0.9 - i * 0.01}
        for i, d in enumerate(doc_ids)
    ]


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):                                    # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = None
        self.server.records.append({                       # type: ignore[attr-defined]
            "body": body, "auth": self.headers.get("Authorization", ""),
        })
        status = self.server.status                        # type: ignore[attr-defined]
        payload = self.server.payload                      # type: ignore[attr-defined]
        self.send_response(status)
        self.end_headers()
        if payload is None:
            self.wfile.write(b"not json at all")
        else:
            self.wfile.write(json.dumps(payload).encode("utf-8"))

    def log_message(self, *args):
        pass


class StubReranker:
    """真的在本地监听的假重排服务（真端口、真请求）。"""

    def __init__(self, payload=None, status: int = 200):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.records = []          # type: ignore[attr-defined]
        self.server.status = status       # type: ignore[attr-defined]
        self.server.payload = payload     # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        return False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/rerank"

    @property
    def records(self) -> list[dict]:
        return self.server.records  # type: ignore[attr-defined]


def _dashscope_payload(pairs: list[tuple[int, float]]) -> dict:
    """按 DashScope 的返回格式造一份结果。"""
    return {"output": {"results": [
        {"index": i, "relevance_score": s} for i, s in pairs
    ]}}


# ============================================================
# 一、开关判定
# ============================================================


class EnabledTestCase(unittest.TestCase):
    def test_disabled_when_key_or_model_missing(self):
        """key 和 model 缺任何一个都算没配好 —— 半个配置等于没配置。"""
        for key, model in [("", ""), ("k", ""), ("", "m"), ("  ", "  ")]:
            with temp_settings(rerank_api_key=key, rerank_model=model):
                self.assertFalse(rr.is_enabled(), f"key={key!r} model={model!r} 时不该认为已启用")
                self.assertIsNone(rr.get_reranker(), "没配置时不该创建重排器")

    def test_enabled_when_both_present(self):
        with temp_settings(rerank_api_key="k", rerank_model="gte-rerank-v2"):
            self.assertTrue(rr.is_enabled())
            self.assertIsNotNone(rr.get_reranker())


# ============================================================
# 二、顺序还原（纯函数，最容易被细节坑到）
# ============================================================


class ReorderTestCase(unittest.TestCase):
    def test_reorders_by_score(self):
        """服务商说第 3 条最相关，就该把它排到第一位。"""
        hits = _hits(1, 2, 3)
        got = rr._order_stably(hits, [(2, 0.9), (0, 0.5), (1, 0.1)])
        self.assertEqual([h["doc_id"] for h in got], [3, 1, 2])

    def test_stable_when_scores_tie(self):
        """分数相同时按下标升序 —— 同样输入两次跑出来必须一模一样。

        不这样做的话，同一个问题每次引用顺序都不同，
        用户会觉得"这系统怎么这么不稳定"，排查时也会以为见鬼了。
        """
        hits = _hits(1, 2, 3, 4)
        a = rr._order_stably(hits, [(0, 0.5), (1, 0.5), (2, 0.5)])
        b = rr._order_stably(hits, [(2, 0.5), (1, 0.5), (0, 0.5)])
        self.assertEqual([h["doc_id"] for h in a], [h["doc_id"] for h in b])
        self.assertEqual([h["doc_id"] for h in a][:3], [1, 2, 3])

    def test_ignores_out_of_range_and_duplicate_index(self):
        """越界下标、重复下标都要丢掉，不能让程序崩。

        各家服务商的实现不一致：有的从 1 开始编号，有的会把同一条返回两次。
        我们只认合法且没出现过的下标，宁可少排几条也不能报错。
        """
        hits = _hits(1, 2, 3)
        got = rr._order_stably(hits, [(1, 0.9), (1, 0.8), (99, 0.7), (-1, 0.6)])
        self.assertEqual([h["doc_id"] for h in got], [2, 1, 3])

    def test_missing_candidates_are_appended(self):
        """服务商只返回了一部分时，剩下的要按原顺序补回来。

        为什么在意：服务商可能受 top_n 限制只返回前几条。
        如果不补回去，开重排反而会让可用引用变少 —— 那就成了"越优化越差"。
        """
        hits = _hits(1, 2, 3, 4, 5)
        got = rr._order_stably(hits, [(2, 0.9)])   # 只返回了一条
        self.assertEqual(len(got), 5, "候选被重排弄丢了")
        self.assertEqual(got[0]["doc_id"], 3)
        self.assertEqual([h["doc_id"] for h in got[1:]], [1, 2, 4, 5])


# ============================================================
# 三、真发一次请求（本地假服务）
# ============================================================


class RerankRequestTestCase(unittest.TestCase):
    def test_request_contains_query_and_documents(self):
        """请求体里必须带问题和候选文本 —— 少了任何一个，重排都无从判断。"""
        with StubReranker(payload=_dashscope_payload([(1, 0.9), (0, 0.2)])) as stub:
            with temp_settings(rerank_api_key="test-key", rerank_model="gte-rerank-v2",
                               rerank_base_url=stub.url, rerank_timeout_seconds=5):
                hits = _hits(1, 2)
                got = _run(rr.rerank_hits("滤芯多久换一次？", hits, top_n=2))

            self.assertTrue(got.applied, got.detail)
            self.assertEqual([h["doc_id"] for h in got.hits], [2, 1])

            rec = stub.records[0]
            self.assertEqual(rec["auth"], "Bearer test-key", "密钥没带过去，服务商一定会拒绝")
            self.assertEqual(rec["body"]["model"], "gte-rerank-v2")
            self.assertEqual(rec["body"]["input"]["query"], "滤芯多久换一次？")
            self.assertEqual(len(rec["body"]["input"]["documents"]), 2)
            self.assertIn("第 1 条内容", rec["body"]["input"]["documents"][0])
            # 让服务商别再回传一遍原文（我们手里已经有了）
            self.assertFalse(rec["body"]["parameters"]["return_documents"])

    def test_scores_under_alt_names_are_accepted(self):
        """有的服务商把分数叫 score、有的叫 relevance_score，两种都要认。"""
        payload = {"output": {"results": [{"index": 1, "score": 0.99}, {"index": 0, "score": 0.1}]}}
        with StubReranker(payload=payload) as stub:
            with temp_settings(rerank_api_key="k", rerank_model="m",
                               rerank_base_url=stub.url, rerank_timeout_seconds=5):
                got = _run(rr.rerank_hits("问", _hits(1, 2), top_n=2))
        self.assertTrue(got.applied)
        self.assertEqual([h["doc_id"] for h in got.hits], [2, 1])


# ============================================================
# 四、失败必须"跳过"，绝不能拖垮问答
# ============================================================


class FailOpenTestCase(unittest.TestCase):
    """这一组是全文件最重要的：重排坏了，用户仍然要能正常提问。

    为什么这么设计：重排只是"锦上添花"（让答案更准），
    而问答本身是"主功能"。为了锦上添花把主功能搞挂，是本末倒置。
    所以任何异常路径都必须退化成"用原顺序继续"。
    """

    def _original_order(self, hits):
        return [h["doc_id"] for h in hits]

    def test_http_500_falls_back(self):
        with StubReranker(payload=_dashscope_payload([(1, 0.9)]), status=500) as stub:
            with temp_settings(rerank_api_key="k", rerank_model="m",
                               rerank_base_url=stub.url, rerank_timeout_seconds=5):
                hits = _hits(1, 2, 3)
                got = _run(rr.rerank_hits("问", hits, top_n=3))
        self.assertFalse(got.applied)
        self.assertIn("500", got.detail)
        self.assertEqual(self._original_order(got.hits), self._original_order(hits),
                         "重排失败时顺序必须保持原样")

    def test_non_json_response_falls_back(self):
        with StubReranker(payload=None) as stub:      # 服务商会返回一段非 JSON 文本
            with temp_settings(rerank_api_key="k", rerank_model="m",
                               rerank_base_url=stub.url, rerank_timeout_seconds=5):
                hits = _hits(1, 2)
                got = _run(rr.rerank_hits("问", hits, top_n=2))
        self.assertFalse(got.applied)
        self.assertEqual(self._original_order(got.hits), self._original_order(hits))

    def test_empty_results_falls_back(self):
        with StubReranker(payload={"output": {"results": []}}) as stub:
            with temp_settings(rerank_api_key="k", rerank_model="m",
                               rerank_base_url=stub.url, rerank_timeout_seconds=5):
                hits = _hits(1, 2)
                got = _run(rr.rerank_hits("问", hits, top_n=2))
        self.assertFalse(got.applied)
        self.assertEqual(self._original_order(got.hits), self._original_order(hits))

    def test_unreachable_service_falls_back(self):
        with temp_settings(rerank_api_key="k", rerank_model="m",
                           rerank_base_url="http://127.0.0.1:1/rerank",   # 没人监听
                           rerank_timeout_seconds=2):
            hits = _hits(1, 2)
            got = _run(rr.rerank_hits("问", hits, top_n=2))
        self.assertFalse(got.applied)
        self.assertEqual(self._original_order(got.hits), self._original_order(hits))

    def test_unexpected_exception_falls_back(self):
        """连我们没预料到的异常也要接住（例如服务商返回了奇怪的嵌套结构）。"""
        payload = {"output": {"results": "这不是列表"}}
        with StubReranker(payload=payload) as stub:
            with temp_settings(rerank_api_key="k", rerank_model="m",
                               rerank_base_url=stub.url, rerank_timeout_seconds=5):
                hits = _hits(1, 2)
                got = _run(rr.rerank_hits("问", hits, top_n=2))
        self.assertFalse(got.applied)
        self.assertEqual(self._original_order(got.hits), self._original_order(hits))

    def test_dirty_single_item_does_not_lose_whole_batch(self):
        """一条脏数据不能让整批结果作废（跳过那条，其余的照常用）。"""
        payload = {"output": {"results": [
            {"index": None, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.8},
            {"index": "x", "relevance_score": 0.7},
        ]}}
        with StubReranker(payload=payload) as stub:
            with temp_settings(rerank_api_key="k", rerank_model="m",
                               rerank_base_url=stub.url, rerank_timeout_seconds=5):
                hits = _hits(1, 2)
                got = _run(rr.rerank_hits("问", hits, top_n=2))
        self.assertTrue(got.applied, "两条脏数据把一条好数据也连累没了")
        self.assertEqual(got.hits[0]["doc_id"], 2)

    def test_empty_hits_short_circuits_without_request(self):
        """没有候选片段时不该去调服务（浪费一次调用，还可能有费用）。"""
        with StubReranker(payload=_dashscope_payload([(0, 1.0)])) as stub:
            with temp_settings(rerank_api_key="k", rerank_model="m",
                               rerank_base_url=stub.url, rerank_timeout_seconds=5):
                got = _run(rr.rerank_hits("问", [], top_n=3))
        self.assertFalse(got.applied)
        self.assertEqual(stub.records, [], "没有候选片段却仍然调用了重排服务")


class RetrieverIntegrationTestCase(unittest.TestCase):
    """重排接进检索链路之后的验证。

    单独测重排函数是不够的 —— 还要确认它**真的被接上了**，
    而且接的位置对（在文档级去噪之前）、坏掉时不会连累整个检索。
    这三件事任何一件出错，前面 15 条重排测试照样全绿，但线上就是没用或直接报错。
    """

    def _patch_pipeline(self, raw_hits: list[dict]):
        """把"向量化"和"向量库检索"换成固定返回，只留中间的去噪/重排真跑。"""
        from app.services import retriever as rt

        async def fake_embed(texts):
            return [[0.1] * 8]

        async def fake_search(query_vector, kb_ids, top_k):
            return [dict(h) for h in raw_hits]

        return (
            unittest.mock.patch.object(rt, "embed_texts", fake_embed),
            unittest.mock.patch.object(rt.vector_store, "search", fake_search),
        )

    def test_disabled_rerank_keeps_vector_order(self):
        """没配重排时，顺序必须还是向量检索给的那个（不能被动过）。"""
        from app.services import retriever as rt

        raw = [
            {"doc_id": 1, "doc_name": "A.pdf", "text": "a", "score": 0.90},
            {"doc_id": 2, "doc_name": "B.pdf", "text": "b", "score": 0.88},
            {"doc_id": 3, "doc_name": "C.pdf", "text": "c", "score": 0.86},
        ]
        p1, p2 = self._patch_pipeline(raw)
        with p1, p2, temp_settings(rerank_api_key="", rerank_model=""):
            got = _run(rt.retrieve("问题", kb_ids=[1], top_k=3))
        self.assertEqual([h["doc_name"] for h in got], ["A.pdf", "B.pdf", "C.pdf"])

    def test_enabled_rerank_changes_order(self):
        """配了重排且服务正常时，顺序要跟着重排结果走。

        造一个"反着排"的假结果：重排说 C 最相关，那 C 就应该排到第一。
        """
        from app.services import retriever as rt

        raw = [
            {"doc_id": 1, "doc_name": "A.pdf", "text": "a", "score": 0.90},
            {"doc_id": 2, "doc_name": "B.pdf", "text": "b", "score": 0.88},
            {"doc_id": 3, "doc_name": "C.pdf", "text": "c", "score": 0.86},
        ]
        p1, p2 = self._patch_pipeline(raw)
        with StubReranker(payload=_dashscope_payload([(2, 0.99), (1, 0.5), (0, 0.1)])) as stub:
            with p1, p2, temp_settings(rerank_api_key="k", rerank_model="m",
                                       rerank_base_url=stub.url, rerank_timeout_seconds=5):
                got = _run(rt.retrieve("问题", kb_ids=[1], top_k=3))
        self.assertEqual([h["doc_name"] for h in got], ["C.pdf", "B.pdf", "A.pdf"],
                         "重排结果没有真正生效（接了但没接上）")

    def test_rerank_does_not_shrink_citation_count(self):
        """**回归守卫**：开了重排之后，引用条数不能变少。

        这是本项目第一版实现真实踩过的坑，值得单独钉一条：
          重排给的是"名次分"（第 1 名 1.0、第 2 名 0.67、第 3 名 0.33……），
          而下游"文档级去噪"还有一道"相对最高分 0.72 倍以下就丢掉"的过滤。
          名次分天然把档次拉开，于是第 2、3 名全被判成"不相关"整篇砍掉 ——
          结果就是**开了重排之后引用反而更少**，花着钱把效果做差了，
          而且不报错、日志还显示"重排成功"，极难发现。
        """
        from app.services import retriever as rt

        raw = [
            {"doc_id": 1, "doc_name": "A.pdf", "text": "a", "score": 0.90},
            {"doc_id": 2, "doc_name": "B.pdf", "text": "b", "score": 0.88},
            {"doc_id": 3, "doc_name": "C.pdf", "text": "c", "score": 0.86},
        ]
        p1, p2 = self._patch_pipeline(raw)
        with StubReranker(payload=_dashscope_payload([(2, 0.99), (1, 0.5), (0, 0.1)])) as stub:
            with p1, p2, temp_settings(rerank_api_key="k", rerank_model="m",
                                       rerank_base_url=stub.url, rerank_timeout_seconds=5):
                got = _run(rt.retrieve("问题", kb_ids=[1], top_k=3))
        self.assertEqual(len(got), 3, "开了重排之后引用条数变少了（越优化越差）")

    def test_rerank_failure_does_not_break_retrieval(self):
        """**关键**：重排服务挂了，检索仍然要正常返回结果。

        这是"重排只是锦上添花、问答才是主功能"在链路层的体现。
        如果这里抛异常，用户会看到"AI 无法回答"，而真正的原因只是
        一个可有可无的优化服务超时了 —— 代价完全不成比例。
        """
        from app.services import retriever as rt

        raw = [
            {"doc_id": 1, "doc_name": "A.pdf", "text": "a", "score": 0.90},
            {"doc_id": 2, "doc_name": "B.pdf", "text": "b", "score": 0.88},
        ]
        p1, p2 = self._patch_pipeline(raw)
        with p1, p2, temp_settings(rerank_api_key="k", rerank_model="m",
                                   rerank_base_url="http://127.0.0.1:1/rerank",
                                   rerank_timeout_seconds=2):
            got = _run(rt.retrieve("问题", kb_ids=[1], top_k=3))
        self.assertEqual([h["doc_name"] for h in got], ["A.pdf", "B.pdf"],
                         "重排挂掉把整个检索带崩了")


if __name__ == "__main__":
    unittest.main()
