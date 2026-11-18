"""Markdown 表格分块的测试。

## 为什么必须有这个测试

这是一个**实测发现并修复的真实缺陷**：

    修复前：问「海联银行全年净利润」→ 答"资料里没有"
    修复后：正确答出「142.7 亿元」

根因不是检索、不是模型，而是**分块**：
递归切分按 500 字符硬切，把表格的行切到了两个片段之间，
关键数据那一行正好落在边界上被切碎。

检索命中了文档，但拿到的片段里没有那个数字。

## 这个 bug 的隐蔽性

  · 文档解析正确
  · 向量化正确
  · 检索逻辑正确
  · 测试全绿

只有"问一个具体数字"才能发现。所以必须把行为固定下来。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.chunker import _is_md_table_separator, _split_md_table, chunk_parse_result


SAMPLE_TABLE = """# 某公司年报

## 主要财务指标

| 指标 | 2025 年 | 2024 年 | 同比变动 |
|---|---|---|---|
| 营业收入（亿元） | 386.2 | 356.3 | +8.4% |
| 归属于母公司股东的净利润（亿元） | 142.7 | 134.5 | +6.1% |
| 每股收益（元） | 1.86 | 1.75 | +6.3% |

## 其他内容

这里是一些正文段落。
"""


class TableSplitTestCase(unittest.TestCase):
    """表格必须按「行 + 表头」切分。"""

    def test_separator_detection(self):
        self.assertTrue(_is_md_table_separator("|---|---|---|"))
        self.assertTrue(_is_md_table_separator("| :--- | ---: | :---: |"))
        self.assertFalse(_is_md_table_separator("| 数据 | 数据 |"))
        self.assertFalse(_is_md_table_separator("普通文本"))

    def test_each_row_has_header(self):
        """每一片都带表头 —— 表格行离开表头就没意义。"""
        pieces = _split_md_table(SAMPLE_TABLE)
        self.assertGreater(len(pieces), 0, "没有识别出表格")

        for p in pieces:
            # 除了纯表头片，其余都应含表头
            if "|" in p and "指标" not in p.split(chr(10))[0]:
                self.assertIn("指标", p, f"片段缺少表头：{p[:60]}")

    def test_key_data_not_split(self):
        """关键数据行必须完整出现在**某一个**片段里。

        这是修复的核心：修复前「净利润 142.7」被切散，
        检索命中文档却拿不到这个数字。
        """
        pieces = _split_md_table(SAMPLE_TABLE)
        hits = [p for p in pieces if "142.7" in p]
        self.assertEqual(len(hits), 1, f"142.7 应恰好出现在 1 个片段，实际 {len(hits)}")

        # 而且那一片要同时含指标名和表头，单独看能读懂
        piece = hits[0]
        self.assertIn("归属于母公司股东的净利润", piece, "片段里没有指标名")
        self.assertIn("指标", piece, "片段里没有表头")

    def test_header_piece_exists(self):
        """表头本身也应作为一个片段（用户可能问"这张表有哪些字段"）。"""
        pieces = _split_md_table(SAMPLE_TABLE)
        header_only = [p for p in pieces if "指标" in p and "142.7" not in p and "386.2" not in p]
        self.assertTrue(header_only, "缺少纯表头片段")

    def test_no_table_returns_empty(self):
        """没有表格时返回空列表，交给普通长文处理。"""
        self.assertEqual(_split_md_table("这是一段没有表格的普通文本。"), [])

    def test_piece_length_bounded(self):
        """单片不能过长（列特别多时要截断）。"""
        wide = "| " + " | ".join(f"列{i}" for i in range(50)) + " |" + chr(10)
        wide += "|" + "---|" * 50 + chr(10)
        wide += "| " + " | ".join("数据" for _ in range(50)) + " |"

        pieces = _split_md_table(wide)
        for p in pieces:
            self.assertLessEqual(len(p), 1000, "片段过长，需要截断")


class ChunkIntegrationTestCase(unittest.TestCase):
    """集成验证：chunk_parse_result 会用新的表格切分。"""

    def test_real_document_produces_table_pieces(self):
        """真实财报文档切出来的片段里，表格行应带表头。"""
        import pathlib

        from app.services.file_parser import normalize_ext, parse_document

        # 用仓库里的真实样例
        sample = pathlib.Path(__file__).parent.parent / "app/domain/finance/samples/01_海联银行2025年度报告.md"
        if not sample.exists():
            self.skipTest("样例文档不存在")

        result = parse_document(str(sample), normalize_ext(sample.name) or "md")
        chunks = chunk_parse_result(result, sample.name)

        # 找含净利润数值的片段
        hits = [c for c in chunks if "142.7" in c.text]
        self.assertTrue(hits, "切出来的片段里找不到 142.7 —— 可能又被切碎了")

        # 该片段应自带表头，能独立理解
        piece = hits[0].text
        self.assertIn("|", piece, "含数据的片段应该还是表格形式")
        self.assertIn("指标", piece, "片段缺少表头，单独看无法理解")


if __name__ == "__main__":
    unittest.main()
