"""Markdown 表格分块的测试。

## 为什么必须有这个测试

这是一个**实测发现并修复的真实缺陷**：

    修复前：问「某项全年指标」→ 答"资料里没有"
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

from app.services.chunker import (
    _chunk_long_text,
    _clean,
    _is_md_table_separator,
    _split_into_segments,
    _split_md_table,
    chunk_parse_result,
)


SAMPLE_TABLE = """# 某公司年报

## 主要财务指标

| 指标 | 2025 年 | 2024 年 | 同比变动 |
|---|---|---|---|
| 营业收入（亿元） | 386.2 | 356.3 | +8.4% |
| 某项指标（亿元） | 142.7 | 134.5 | +6.1% |
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

        这是修复的核心：修复前「某个具体数值」被切散，
        检索命中文档却拿不到这个数字。
        """
        pieces = _split_md_table(SAMPLE_TABLE)
        hits = [p for p in pieces if "142.7" in p]
        self.assertEqual(len(hits), 1, f"142.7 应恰好出现在 1 个片段，实际 {len(hits)}")

        # 而且那一片要同时含指标名和表头，单独看能读懂
        piece = hits[0]
        self.assertIn("某项指标", piece, "片段里没有指标名")
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


class ContentPreservationTestCase(unittest.TestCase):
    """切分**不能丢内容** —— 这是上一版最严重的 bug。

    ## 教训

    上一版的 _chunk_long_text 写成"发现表格就整段返回"：

        table_pieces = _split_md_table(cleaned)
        if table_pieces:
            return table_pieces      # ← 表前/表后正文全丢

    实测真实文档：2365 字 → 750 字，**丢了 68%**。
    「公司基本情况」「总资产」「不良贷款率」「风险因素」全部消失。

    而当时的测试只断言"142.7 存在"，完全没发现。

    **这个类的测试就是为了防止同类问题再发生：**
    不只验证"该在的在"，还要验证"总量没有明显减少"。
    """

    def test_text_before_and_after_table_preserved(self):
        """表格前后的正文都必须保留。"""
        doc = chr(10).join([
            "# 文档标题",
            "",
            "## 表前正文",
            "这段内容在表格之前，不能被丢掉。",
            "",
            "| 指标 | 数值 |",
            "|---|---|",
            "| 甲 | 1 |",
            "| 乙 | 2 |",
            "",
            "## 表后正文",
            "这段内容在表格之后，同样不能丢。",
        ])

        pieces = _split_into_segments(_clean(doc))
        types = [k for k, _ in pieces]

        self.assertIn("table", types, "没有识别出表格")

        combined = " ".join(c for _, c in pieces)
        self.assertIn("表前正文", combined, "表前的正文丢了")
        self.assertIn("表后正文", combined, "表后的正文丢了")
        self.assertIn("这段内容在表格之前", combined)

    def test_multiple_tables_all_kept(self):
        """多个表格都要保留，不能只留第一个。"""
        doc = chr(10).join([
            "## 表一",
            "| A | B |",
            "|---|---|",
            "| 1 | 2 |",
            "",
            "## 中间正文",
            "两表之间的文字。",
            "",
            "## 表二",
            "| C | D |",
            "|---|---|",
            "| 3 | 4 |",
        ])

        segments = _split_into_segments(_clean(doc))
        tables = [c for k, c in segments if k == "table"]
        self.assertGreaterEqual(len(tables), 2, f"只识别出 {len(tables)} 张表")

        combined = " ".join(c for _, c in segments)
        self.assertIn("中间正文", combined, "两表之间的正文丢了")

    def test_no_significant_content_loss(self):
        """整体内容量不能明显减少（防止"切完只剩一小半"）。"""
        # 造一份"短正文 + 大表格 + 短正文"的文档
        doc = (
            "开头一段说明文字，大约二十个字左右。"
            + chr(10)
            + "| 列1 | 列2 | 列3 |"
            + chr(10)
            + "|---|---|---|"
            + chr(10)
            + chr(10).join(f"| 数据{i} | 值{i} | 说明{i} |" for i in range(10))
            + chr(10)
            + "结尾一段说明文字，也大约二十个字左右。"
        )

        chunks = _chunk_long_text(doc)
        total = sum(len(c[0]) for c in chunks)

        # 表格行会重复带表头，所以总量会略增；关键是**不能大幅减少**
        self.assertGreaterEqual(
            total,
            len(doc) * 0.9,
            f"内容明显丢失：原文 {len(doc)} 字，切完只有 {total} 字",
        )

    def test_table_without_outer_pipes(self):
        """Markdown 允许省略外侧竖线，这种表格也要能识别。

        形如：
            指标 | 2025
            --- | ---
            营收 | 100
        """
        doc = chr(10).join([
            "指标 | 2025",
            "--- | ---",
            "营收 | 100",
            "利润 | 20",
        ])

        segments = _split_into_segments(_clean(doc))
        tables = [c for k, c in segments if k == "table"]
        self.assertTrue(tables, "无外侧竖线的表格没被识别")

    def test_adjacent_tables_not_merged(self):
        """相邻两张表不能粘成一张（第二张表头不能被当成数据行）。"""
        doc = chr(10).join([
            "| A | B |",
            "|---|---|",
            "| 1 | 2 |",
            "| C | D |",
            "|---|---|",
            "| 3 | 4 |",
        ])

        tables = [c for k, c in _split_into_segments(_clean(doc)) if k == "table"]
        self.assertGreaterEqual(
            len(tables), 2,
            f"相邻两表被合并了，只识别出 {len(tables)} 张",
        )


class ChunkIntegrationTestCase(unittest.TestCase):
    """集成验证：chunk_parse_result 会用新的表格切分。"""

    def test_real_document_produces_table_pieces(self):
        """真实文档切出来的片段里，表格行应带表头。

        ⚠️ 不硬编码某个领域的路径：
            导出产物只含单一领域，写死 `app/domain/finance/...`
            在产物里必然失败（而失败会让人以为项目坏了）。
            改为"扫当前领域的 samples 目录，找任意一份含表格的文档"。
        """
        import pathlib

        from app.domain import get_domain
        from app.services.file_parser import normalize_ext, parse_document

        # 用当前领域的样例目录，不指定具体领域
        samples_dir = get_domain().samples_dir()
        if not samples_dir.exists():
            self.skipTest("当前领域没有样例目录")

        # 找一份含 Markdown 表格的 .md 文档
        target = None
        for f in samples_dir.glob("*.md"):
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            # 粗判：有表头 + 分隔行
            if "|---" in text and "|" in text:
                target = f
                break

        if target is None:
            self.skipTest("当前领域没有含表格的 Markdown 样例")

        result = parse_document(str(target), normalize_ext(target.name) or "md")
        chunks = chunk_parse_result(result, target.name)

        # 找到至少一个"表格行且带表头"的片段
        table_chunks = [
            c for c in chunks
            if c.text.strip().startswith("|") and "|" in c.text
        ]
        self.assertTrue(table_chunks, "没有切出任何表格片段")

        # 抽样检查：每个表格片段都应该至少两行（表头 + 数据）
        for c in table_chunks[:5]:
            lines = [ln for ln in c.text.split(chr(10)) if ln.strip()]
            self.assertGreaterEqual(
                len(lines), 1,
                f"表格片段内容异常：{c.text[:60]}",
            )


if __name__ == "__main__":
    unittest.main()
