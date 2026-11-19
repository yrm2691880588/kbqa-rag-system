# -*- coding: utf-8 -*-
"""给 Code Review 修的两个严重 bug 补回归测试。

## bug 1：筛选列名写错会静默返回全表

原实现：
    if i is None:
        continue      # 静默跳过这个条件

实测后果：把「公司名称」写成「公司名称 」（多空格），
sum 返回 ok=True，把三家公司 9 行一起加总（531.7 而非 60）。
**模型会拿这个错数去回答用户。**

修后：列名对不上直接返回 ok=False + 可用列名清单。

## bug 2：重传同一份文件走不到覆盖分支

原顺序：内容哈希查重（先） → 同名检测（后）
导致最常见的"重传同一份文件"被内容查重挡住，
文案还写着"请先删除旧文件再上传"——正是需求要解决的痛点。

修后：同名检测提前；同名+内容相同 → 直接返回现有记录。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class BadColumnTestCase(unittest.TestCase):
    """筛选列名写错必须报错，不能静默返回全表。"""

    def test_bad_column_raises(self):
        """列名不存在时 _filter_rows 要抛 _BadColumn。"""
        from app.tools.table_query import TableQueryTool, _BadColumn

        tool = TableQueryTool()
        header = ["主体", "期间", "指标A"]
        idx = {h: i for i, h in enumerate(header)}
        rows = [["甲", "Q1", "100"], ["乙", "Q2", "200"]]

        with self.assertRaises(_BadColumn) as ctx:
            tool._filter_rows(
                header, rows, {"主体 ": "甲"}, idx   # 多一个空格
            )
        self.assertEqual(ctx.exception.column, "主体 ")
        self.assertIn("主体", ctx.exception.available)

    def test_correct_column_works(self):
        """列名正确时正常筛选。"""
        from app.tools.table_query import TableQueryTool

        tool = TableQueryTool()
        header = ["主体", "期间", "指标A"]
        idx = {h: i for i, h in enumerate(header)}
        rows = [["甲", "Q1", "100"], ["乙", "Q2", "200"]]

        out = tool._filter_rows(header, rows, {"主体": "甲"}, idx)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "甲")

    def test_no_filters_returns_all(self):
        """不传条件返回全部。"""
        from app.tools.table_query import TableQueryTool

        tool = TableQueryTool()
        header = ["主体", "期间", "指标A"]
        idx = {h: i for i, h in enumerate(header)}
        rows = [["甲", "Q1", "100"], ["乙", "Q2", "200"]]

        out = tool._filter_rows(header, rows, {}, idx)
        self.assertEqual(len(out), 2)

    def test_array_value_matches_any(self):
        """条件是数组时，命中任一即可。"""
        from app.tools.table_query import TableQueryTool

        tool = TableQueryTool()
        header = ["主体", "期间", "指标A"]
        idx = {h: i for i, h in enumerate(header)}
        rows = [
            ["甲", "Q1", "100"],
            ["甲", "Q2", "200"],
            ["甲", "Q3", "300"],
            ["乙", "Q1", "400"],
        ]

        out = tool._filter_rows(header, rows, {"主体": "甲", "期间": ["Q1", "Q2"]}, idx)
        self.assertEqual(len(out), 2, f"应命中 2 行，实际 {len(out)}")


class UploadOrderTestCase(unittest.TestCase):
    """上传流程的顺序：同名检测必须在内容查重之前。"""

    def test_same_name_check_before_hash_check(self):
        """同名检测的代码位置必须在内容哈希查重之前。

        ⚠️ 这条保留源码位置检查是有意的：
            真正的顺序验证需要完整的上传流程（HTTP + 文件系统 + 队列），
            单元测试里跑不动。

            但"顺序"恰恰是 bug 的本质 —— 用位置断言抓到它，
            比完全没有保护强。改动这段代码时如果顺序又变回去，会红。
        """
        import inspect

        from app.api.v1 import knowledge_bases

        src = inspect.getsource(knowledge_bases._save_one_document)
        pos_same_name = src.find("same_name_docs = [")
        pos_hash_dedup = src.find("Document.file_hash == file_hash")

        self.assertGreater(pos_same_name, 0, "找不到同名检测")
        self.assertGreater(pos_hash_dedup, 0, "找不到内容查重")
        self.assertLess(
            pos_same_name, pos_hash_dedup,
            "内容查重排在同名检测之前 —— 重传同名文件会被挡住，"
            "走不到版本更新分支（这是 Code Review 抓到的真实 bug）",
        )

    def test_unchanged_content_returns_existing(self):
        """同名且内容相同时，应直接返回现有记录而不是报错。"""
        import inspect

        from app.api.v1 import knowledge_bases

        src = inspect.getsource(knowledge_bases._save_one_document)
        self.assertIn(
            "target.file_hash == file_hash", src,
            "缺少「同名+内容相同」的分支 —— 用户重传同一份文件会收到 409",
        )

    def test_filename_stem_extracted(self):
        """「去扩展名」逻辑必须是共享函数，不能在多处重复。"""
        from app.api.v1.knowledge_bases import _filename_stem

        # 直接测生产函数（而不是在测试里重写一份）
        self.assertEqual(_filename_stem("资料.md"), "资料")
        self.assertEqual(_filename_stem("资料"), "资料")
        self.assertEqual(_filename_stem("v1.2资料.md"), "v1.2资料")
        self.assertEqual(_filename_stem(""), "")


class ExportTruncationTestCase(unittest.TestCase):
    """导出截断：取最近 N 条，且有响应头标记。"""

    def test_takes_latest_not_earliest(self):
        """超限时应该保留最近的消息，不是最早的。

        原实现 `order_by(id.asc()).limit(N)` 截掉的是最新消息，
        用户导出后发现"最近问的问题不在里面"，和直觉相反。
        """
        import inspect

        from app.api.v1 import sessions

        src = inspect.getsource(sessions.export_session)
        self.assertIn(
            "order_by(Message.id.desc())", src,
            "没按 id 降序取 —— 超限时截掉的是最新消息",
        )
        self.assertIn("rows.reverse()", src, "取完最近 N 条后要反转成正序")

    def test_sets_truncation_header(self):
        """注释承诺了截断标记，必须真的设置。"""
        import inspect

        from app.api.v1 import sessions

        src = inspect.getsource(sessions.export_session)
        self.assertIn(
            "X-Export-Truncated", src,
            "没设置截断响应头 —— 前端无法提示用户被截断了",
        )


if __name__ == "__main__":
    unittest.main()
