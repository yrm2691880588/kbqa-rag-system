"""检索文档级去噪模块单元测试（retriever.py 的 _doc_level_denoise）。

被测函数是纯函数：输入命中片段列表，输出过滤后的片段列表。
它只依赖 settings 里的两个阈值：retrieval_margin_ratio（文档级相对过滤倍率）
与 retrieval_chunks_per_doc（同文档最多保留条数）。

运行方式（在 backend 目录下）：
    C:\\Users\\pjs\\.venvs\\kbqa\\Scripts\\python.exe -m unittest tests.test_retriever -v
"""
import unittest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.retriever import _doc_level_denoise


def hit(doc_id, score, text="片段内容"):
    """构造一条检索命中片段。"""
    return {"doc_id": doc_id, "doc_name": f"文档{doc_id}", "score": score, "text": text}


class TestDocLevelDenoise(unittest.TestCase):
    """_doc_level_denoise(hits) -> list[dict] 的测试组。

    设计背景：问"净化器A8面积"时，价格表(0.77)与手表手册(0.45)都过了绝对阈值，
    但手表与问题无关，需要靠"相对分差"把垫底文档整篇丢掉。
    """

    # ---------- 正常用例 ----------

    def test_单文档多片段全保留_数量受限(self):
        """只来自一篇文档的多个片段都相关，保留（不误删同文档内容）。"""
        hits = [hit(1, 0.90), hit(1, 0.88), hit(1, 0.85)]
        out = _doc_level_denoise(hits)
        self.assertTrue(out, "不应把唯一的文档全部丢掉")
        self.assertLessEqual(len(out), 2, "同文档最多保留 2 条（chunks_per_doc=2）")

    def test_低分文档整篇丢弃(self):
        """价格表 0.77 与手表手册 0.45 分差明显 → 只保留价格表，手表整篇丢弃。"""
        hits = [
            hit(1, 0.77),  # 价格表：真命中
            hit(2, 0.45),  # 手表手册：弱沾边，应整篇丢弃
            hit(3, 0.40),  # 手机手册：更弱，丢弃
        ]
        out = _doc_level_denoise(hits)
        self.assertEqual([h["doc_id"] for h in out], [1])

    def test_高分文档的多片段仍保留(self):
        """高分文档内多个接近最高分的片段应保留（对回答有用）。"""
        hits = [
            hit(1, 0.90, "片段A"),
            hit(1, 0.87, "片段B"),
            hit(1, 0.70, "片段C"),  # 低于 0.90*0.85=0.765 → 应被文档内过滤
            hit(2, 0.80, "片段D"),  # 0.80 < 0.90*0.72=0.648? 不，0.80>=0.648 应保留
        ]
        out = _doc_level_denoise(hits)
        doc_ids = {h["doc_id"] for h in out}
        self.assertIn(1, doc_ids)
        self.assertIn(2, doc_ids)
        self.assertNotIn("片段C", [h["text"] for h in out], "同文档内低于最高分 85% 的碎片应去掉")

    # ---------- 边界用例 ----------

    def test_空列表返回空(self):
        """空输入应返回空列表，不崩溃。"""
        self.assertEqual(_doc_level_denoise([]), [])

    def test_单条命中原样返回(self):
        """只有一条命中时保留它。"""
        out = _doc_level_denoise([hit(1, 0.60)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["doc_id"], 1)

    def test_分数全相同保留全部文档(self):
        """两篇文档最高分一样 → 都算"同样相关"，不该互相淘汰。"""
        hits = [hit(1, 0.80), hit(2, 0.80)]
        out = _doc_level_denoise(hits)
        self.assertEqual({h["doc_id"] for h in out}, {1, 2})

    def test_输出按分数降序(self):
        """输出列表应按相似度从高到低排序。"""
        hits = [hit(1, 0.50), hit(2, 0.95), hit(3, 0.99)]
        out = _doc_level_denoise(hits)
        scores = [h["score"] for h in out]
        self.assertEqual(scores, sorted(scores, reverse=True), "输出应按分数降序")

    def test_缺失score的片段按0处理不崩溃(self):
        """脏数据（无 score 字段）不应导致崩溃。"""
        hits = [{"doc_id": 1, "text": "无分数片段"}, hit(2, 0.90)]
        out = _doc_level_denoise(hits)
        self.assertTrue(out)


if __name__ == "__main__":
    unittest.main()
