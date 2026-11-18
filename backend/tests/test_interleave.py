"""交错排序的测试。

## 为什么这个行为值得测

实测发现：纯按分数排序时，**同一篇文档的多个片段会占满前几位**。

改进前的评估数据（3 个检索失败的共同病根）：

    某个指标: [xlsx, xlsx, 分季度]      年报排第 5 → Hit@3 判为未命中
    口径:   [xlsx, 年报, 年报]        口径详解排第 4
    风险:   [行业报告, 行业报告, xlsx]

答案可能只在第 2 篇文档里，但前 3 名被第 1 篇占满。

改成交错排序后：Hit@3 从 88% → 100%（实测）。

如果将来有人"优化"回纯按分数排，这个测试会拦住。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.retriever import _doc_level_denoise


def hit(doc_id: int, score: float, text: str = "x") -> dict:
    return {
        "doc_id": doc_id,
        "doc_name": f"文档{doc_id}.md",
        "score": score,
        "text": text,
    }


class InterleaveTestCase(unittest.TestCase):
    """交错排序的核心行为。"""

    def test_top3_from_different_docs(self):
        """前 3 条应来自不同文档（当有多篇可用时）。

        构造场景：文档 1 有 3 条高分片段（0.9/0.88/0.86），
        文档 2 只有 1 条（0.70）。

        纯按分数排：[1,1,1] —— 前 3 全是文档 1
        交错排：    [1,2,...] —— 前 3 覆盖 2 篇文档
        """
        hits = [
            hit(1, 0.90), hit(1, 0.88), hit(1, 0.86),
            hit(2, 0.70),
        ]
        out = _doc_level_denoise(hits)
        docs = [h["doc_id"] for h in out[:3]]

        self.assertGreaterEqual(
            len(set(docs)), 2,
            f"前 3 条只来自 {len(set(docs))} 篇文档（{docs}）—— "
            f"交错排序失效，会把答案挤到后面",
        )

    def test_first_layer_has_all_docs(self):
        """第一层（各文档最高分）应覆盖所有保留下来的文档。"""
        hits = [
            hit(1, 0.90), hit(1, 0.80),
            hit(2, 0.70), hit(2, 0.60),
        ]
        out = _doc_level_denoise(hits)
        self.assertGreaterEqual(len(out), 2)

        first_two = [h["doc_id"] for h in out[:2]]
        self.assertEqual(
            set(first_two), {1, 2},
            f"第一层应包含两篇文档各一条，实际 {first_two}",
        )

    def test_scores_descending_within_layer(self):
        """同一层内按分数降序（高分优先）。"""
        hits = [
            hit(1, 0.90), hit(2, 0.80),
            hit(3, 0.70),
        ]
        out = _doc_level_denoise(hits)
        if len(out) >= 2:
            self.assertGreaterEqual(
                out[0]["score"], out[1]["score"],
                "同层内应按分数降序",
            )

    def test_single_doc_still_works(self):
        """只有一篇文档时不崩，且内容不丢。"""
        hits = [hit(1, 0.90), hit(1, 0.80)]
        out = _doc_level_denoise(hits)
        self.assertTrue(out, "不应把唯一的文档丢掉")
        self.assertTrue(all(h["doc_id"] == 1 for h in out))

    def test_no_extra_chunks_invented(self):
        """交错只是重排，不能凭空多出片段。"""
        hits = [
            hit(1, 0.90), hit(1, 0.85),
            hit(2, 0.80), hit(2, 0.75),
            hit(3, 0.70),
        ]
        out = _doc_level_denoise(hits)
        self.assertGreater(len(out), 0)
        self.assertLessEqual(len(out), len(hits), "输出不应比输入还多")

    def test_empty_input(self):
        self.assertEqual(_doc_level_denoise([]), [])


if __name__ == "__main__":
    unittest.main()
