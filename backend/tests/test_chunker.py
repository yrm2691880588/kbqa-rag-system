"""智能切块模块单元测试（chunker.py）——验证文本清洗与分片是否正确。

被测对象：
- _clean(text)：压缩空白、剔除装饰性符号行
- _chunk_long_text(text)：短文不分片；超长文按语义断点分片并保留页码
- chunk_parse_result(result)：把解析结果切成带连续序号的 Chunk 列表

运行方式（在 backend 目录下）：
    python -m unittest tests.test_chunker -v
"""
import unittest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.chunker import _clean, _chunk_long_text, chunk_parse_result
from app.services.file_parser import ParseResult, ParsedUnit


class TestClean(unittest.TestCase):
    """_clean(text) 文本清洗测试。"""

    def test_压缩连续空格与全角空格(self):
        """连续半角/全角空格应合并为单个空格。"""
        self.assertEqual(_clean("你好    世界\u3000啊"), "你好 世界 啊")

    def test_剔除纯装饰行(self):
        """只有横线/星号/井号等装饰符号的行应被去掉。"""
        cleaned = _clean("标题\n——\n内容")
        self.assertNotIn("——", cleaned)
        self.assertIn("标题", cleaned)
        self.assertIn("内容", cleaned)

    def test_空文本返回空串(self):
        """只有空白或装饰的输入返回空串（调用方据此跳过）。"""
        self.assertEqual(_clean("   "), "")
        self.assertEqual(_clean("\n\n"), "")


class TestChunkLongText(unittest.TestCase):
    """_chunk_long_text(text) 分片测试。"""

    def test_短文不分片直接返回(self):
        """短于阈值(约600字)的长文本应整体保留为 1 片。"""
        short = "星耀A8净化器适用45平方米" * 3  # 约 60 字
        out = _chunk_long_text(short, page=1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], 1, "页码应透传")

    def test_空文本返回空列表(self):
        self.assertEqual(_chunk_long_text("   "), [])

    def test_超长文被切为多片且页码保留(self):
        """1500+ 字的文本应切成多片，且每片带原页码。"""
        long_text = "产品规格说明。" * 300  # 约 1800 字
        out = _chunk_long_text(long_text, page=2)
        self.assertGreater(len(out), 1, "超长文应切成多片")
        for text, page, section in out:
            self.assertEqual(page, 2)
            self.assertTrue(text.strip(), "分出的每片不应为空")

    def test_分片合计基本覆盖原文(self):
        """所有分片拼接应能覆盖原文大部分内容（允许重叠/略损）。"""
        long_text = "星耀A8空气净化器采用三重过滤技术。" * 120
        out = _chunk_long_text(long_text, page=3)
        joined = "".join(t for t, _, _ in out)
        self.assertIn("星耀A8空气净化器", joined)


class TestChunkParseResult(unittest.TestCase):
    """chunk_parse_result(result) 集成测试。"""

    def _parse_text(self, *units_text):
        """构造一个 kind=flow 的解析结果。"""
        units = [ParsedUnit(text=t, page=i + 1, section="规格") for i, t in enumerate(units_text)]
        return ParseResult(kind="flow", units=units, warnings=[])

    def test_序号从0连续递增(self):
        """多单元切出的 Chunk 序号应 0,1,2,... 连续。"""
        result = self._parse_text("星跃S10电池5000mAh快充120W", "星翼耳机支持IPX5防水")
        chunks = chunk_parse_result(result, "测试.md")
        self.assertEqual([c.index for c in chunks], list(range(len(chunks))))

    def test_每片保留来源页(self):
        result = self._parse_text("第一单元内容", "第二单元内容")
        chunks = chunk_parse_result(result, "测试.md")
        self.assertEqual(chunks[0].page, 1)
        self.assertEqual(chunks[1].page, 2)

    def test_空解析结果返回空列表(self):
        result = ParseResult(kind="flow", units=[], warnings=[])
        self.assertEqual(chunk_parse_result(result, "空.md"), [])

    def test_表格解析整行一片(self):
        """Excel 表格场景：一行商品 = 一片，不拆散。"""
        unit = ParsedUnit(text="星跃S10 电池5000mAh 快充120W 价格3999", page=None, section="Sheet1")
        result = ParseResult(kind="table", units=[unit], warnings=[])
        chunks = chunk_parse_result(result, "价格表.xlsx")
        self.assertEqual(len(chunks), 1, "单行商品应为一个整片")
        self.assertIn("电池5000mAh", chunks[0].text)


if __name__ == "__main__":
    unittest.main()
