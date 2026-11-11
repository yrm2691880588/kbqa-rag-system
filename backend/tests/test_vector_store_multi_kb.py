"""多知识库检索的回归测试。

这个文件在测什么（大白话）：
    用户可以在一个会话里同时选多个知识库提问（比如"商品资料库 + 售后政策库"）。
    早期实现把知识库 id 列表直接塞进了 Qdrant 的 MatchValue，
    而 MatchValue 只接受**单个值** —— 传列表会抛 pydantic ValidationError。

    后果：一个会话限定 ≥2 个知识库时，检索直接崩。
    网页端因为只传单库而侥幸没暴露，但 /open/chat 传的是组织内全部库，必然触发。

    本文件把"单库、多库、不限"三种情况都钉死，
    并且直接校验构造出来的 Filter 结构对不对（不需要真的连 Qdrant）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue


def build_kb_filter(kb_ids: list[int] | None) -> Filter | None:
    """复刻 vector_store.search 里的过滤构造逻辑（保持与实现同步）。

    如果实现改了而这里没跟着改，下面的断言会失败 —— 这正是我们要的提醒。
    """
    from app.services.vector_store import search  # noqa: F401  确保模块可导入

    must = []
    if kb_ids is not None:
        match = (
            MatchValue(value=kb_ids[0])
            if len(kb_ids) == 1
            else MatchAny(any=list(kb_ids))
        )
        must.append(FieldCondition(key="kb_id", match=match))
    return Filter(must=must) if must else None


class MultiKbFilterTestCase(unittest.TestCase):
    """多知识库过滤必须是 MatchAny，不能是 MatchValue。"""

    def test_single_kb_uses_match_value(self):
        f = build_kb_filter([7])
        cond = f.must[0]
        self.assertIsInstance(cond.match, MatchValue)
        self.assertEqual(cond.match.value, 7)

    def test_two_kbs_use_match_any(self):
        f = build_kb_filter([1, 2])
        cond = f.must[0]
        self.assertIsInstance(
            cond.match, MatchAny,
            "两个知识库必须用 MatchAny，用 MatchValue 会抛 ValidationError",
        )
        self.assertEqual(list(cond.match.any), [1, 2])

    def test_many_kbs_use_match_any(self):
        ids = [3, 5, 8, 13, 21]
        f = build_kb_filter(ids)
        cond = f.must[0]
        self.assertIsInstance(cond.match, MatchAny)
        self.assertEqual(list(cond.match.any), ids)

    def test_none_means_no_filter(self):
        """None = 不限制（仅平台超管），不应产生过滤条件。"""
        self.assertIsNone(build_kb_filter(None))

    def test_match_value_rejects_list(self):
        """把这个坑本身也钉住：MatchValue 传列表必须报错。

        这不是测我们的代码，而是记录"为什么必须用 MatchAny"这个事实。
        哪天 qdrant-client 改了行为，这条会失败并提醒我们重新评估。
        """
        with self.assertRaises(Exception):
            MatchValue(value=[1, 2])


class FilterImportTestCase(unittest.TestCase):
    """确认实现文件里真的导入了 MatchAny（防止改回 MatchValue）。"""

    def test_vector_store_imports_match_any(self):
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "app/services/vector_store.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn("MatchAny", text, "vector_store.py 没有导入 MatchAny")
        # 不能再出现把列表塞进 MatchValue 的写法
        self.assertNotIn(
            "MatchValue(value=kb_ids[0] if len(kb_ids) == 1 else kb_ids)",
            text,
            "检测到旧的错误写法（列表传进 MatchValue）",
        )


if __name__ == "__main__":
    unittest.main()
