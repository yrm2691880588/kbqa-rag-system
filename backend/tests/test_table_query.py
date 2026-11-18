"""表格结构化查询工具的测试。

## 这个工具解决什么问题

向量检索对"表格里的一行"区分度很低。实测两个典型失败：

    「2025 年前三季度营业收入合计」
      → 需要 Q1+Q2+Q3 相加，检索做不到"聚合"

    「哪家公司的人最多」
      → 答案 `8642` 是纯数字，Qdrant 全文索引根本匹配不到纯数字

**这不是"检索不准"，而是检索这种手段本身做不到。**

## 做法与边界

工具直接读原始 Excel，只暴露四个受限操作：
list_tables / describe / query / sum。

刻意不做 Text-to-SQL：模型写错 SQL 时很难自我纠正，
而"从固定选项里选"不会语法错误。

## 安全边界（这组测试要守住）

  · 只读不写
  · 文件路径由 doc_file_path(kb_id, doc_id, ext) 生成，不接受模型传的路径
  · 单元格数上限（防上下文爆炸）
  · 失败返回错误文本，不抛异常
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tools.table_query import TableQueryTool, _looks_numeric, _to_number


class NumberParsingTestCase(unittest.TestCase):
    """数字解析（求和的基础）。"""

    def test_plain_number(self):
        self.assertEqual(_to_number("142.7"), 142.7)
        self.assertEqual(_to_number("8642"), 8642.0)

    def test_with_thousand_separator(self):
        """带千分位逗号要能认出来（真实 Excel 常见）。"""
        self.assertEqual(_to_number("8,642"), 8642.0)
        self.assertEqual(_to_number("1,234,567.8"), 1234567.8)

    def test_with_percent(self):
        """带百分号也要能转（用于对百分比列求和）。"""
        self.assertEqual(_to_number("105%"), 105.0)

    def test_empty_and_text(self):
        self.assertIsNone(_to_number(""))
        self.assertIsNone(_to_number("   "))
        self.assertIsNone(_to_number("不适用"))

    def test_looks_numeric(self):
        self.assertTrue(_looks_numeric("123"))
        self.assertTrue(_looks_numeric("1,234"))
        self.assertFalse(_looks_numeric("备注文字"))


class ToolContractTestCase(unittest.TestCase):
    """工具是否符合 Tool 接口约定。"""

    def setUp(self):
        self.tool = TableQueryTool()

    def test_has_required_members(self):
        """必须提供 name/description/parameters/run 四个成员。"""
        self.assertTrue(self.tool.name)
        self.assertTrue(self.tool.description)
        self.assertIsInstance(self.tool.parameters, dict)
        self.assertTrue(callable(self.tool.run))

    def test_name_is_snake_case(self):
        """工具名要小写+下划线（约定）。"""
        self.assertRegex(self.tool.name, r"^[a-z][a-z0-9_]*$")

    def test_declares_needs_context(self):
        """必须声明需要上下文。

        为什么重要：这个工具需要"当前库有哪些 Excel"才能工作。
        不声明的话，rag.py 不会传上下文，工具会永远说"没有可查询的表格"。
        """
        self.assertTrue(
            getattr(self.tool, "needs_context", False),
            "没声明 needs_context，rag.py 不会传上下文，工具会失效",
        )

    def test_description_mentions_when_to_use(self):
        """描述里要写清"什么时候该用"（实测这条对模型影响很大）。"""
        d = self.tool.description
        self.assertIn("当", d, "描述里没说明使用时机")
        self.assertIn("不要", d, "描述里没说明什么时候不该用")

    def test_parameters_schema_valid(self):
        """参数字段结构必须合法。"""
        p = self.tool.parameters
        self.assertEqual(p.get("type"), "object")
        self.assertIn("properties", p)
        self.assertIn("action", p["properties"])
        self.assertIn("action", p.get("required", []))

    def test_action_enum_restricted(self):
        """action 必须是枚举，不能是自由字符串。

        这是"做窄"的核心：模型只能从固定选项里选，
        不会像写 SQL 那样写错语法。
        """
        act = self.tool.parameters["properties"]["action"]
        self.assertIn("enum", act)
        self.assertEqual(
            set(act["enum"]),
            {"list_tables", "describe", "query", "sum"},
        )


class ToolBehaviourTestCase(unittest.TestCase):
    """工具行为（不需要真实文件的部分）。"""

    def setUp(self):
        self.tool = TableQueryTool()

    def test_no_tables_returns_graceful_error(self):
        """没有表格文件时返回友好的错误，不崩溃。"""
        import asyncio

        r = asyncio.run(
            self.tool.run(action="list_tables", _context={"tables": []})
        )
        self.assertFalse(r.ok)
        self.assertIn("没有", r.text)

    def test_missing_action(self):
        import asyncio

        r = asyncio.run(self.tool.run(_context={"tables": []}))
        self.assertFalse(r.ok)
        self.assertIn("action", r.text)

    def test_unknown_action(self):
        import asyncio

        r = asyncio.run(
            self.tool.run(action="drop_table", _context={"tables": []})
        )
        self.assertFalse(r.ok)

    def test_sum_without_column(self):
        """sum 缺 column 时给明确错误。"""
        import asyncio

        ctx = {"tables": [{"kb_id": 1, "doc_id": 1, "name": "x.xlsx"}]}
        r = asyncio.run(
            self.tool.run(action="sum", table="t", _context=ctx)
        )
        # 文件不存在时先报读取失败，也是 ok=False —— 关键是别崩
        self.assertFalse(r.ok)


class SecurityTestCase(unittest.TestCase):
    """安全边界：不能接受模型传入的任意路径。"""

    def test_no_path_parameter(self):
        """参数里不能有 path/file/filename 这类字段。

        为什么重要：如果模型能传路径，就可能读到知识库以外的文件
        （路径穿越）。文件必须由 doc_id 推导，不能由模型指定。
        """
        props = TableQueryTool.parameters["properties"]
        for dangerous in ("path", "file", "filename", "filepath", "dir"):
            self.assertNotIn(
                dangerous, props,
                f"参数里出现 {dangerous} —— 模型可能借此读任意文件",
            )

    def test_only_doc_id_and_name_in_context(self):
        """上下文里只放 doc_id 和文件名，不放绝对路径。

        代码层面验证：_build_tool_context 构造的字典键
        应该只有 kb_id / doc_id / name。
        """
        import inspect

        from app.services import rag

        src = inspect.getsource(rag._build_tool_context)
        self.assertIn('"doc_id"', src)
        self.assertIn('"name"', src)
        # 不能出现把 path 塞进上下文的写法
        self.assertNotIn('"path":', src, "上下文里不该有 path")

    def test_max_rows_limits_output(self):
        """必须限制单次返回行数（防上下文爆炸）。"""
        from app.tools import table_query

        self.assertTrue(
            hasattr(table_query, "_MAX_ROWS"),
            "缺少行数上限",
        )
        self.assertGreater(table_query._MAX_ROWS, 0)
        self.assertLessEqual(
            table_query._MAX_ROWS, 200,
            f"上限 {table_query._MAX_ROWS} 太大，可能撑爆上下文",
        )


class RegistryTestCase(unittest.TestCase):
    """工具注册与领域配置。"""

    def test_registered_in_builtin(self):
        from app.tools.registry import available_tool_names

        self.assertIn("table_query", available_tool_names())

    def test_finance_domain_enables_it(self):
        """财经领域应该启用这个工具。

        ⚠️ 导出产物里只有单一领域（finance 目录被排除），
        这时 import 会失败 —— 应该跳过而不是报错。
        和评估集测试是同一类问题，踩过一次了。
        """
        try:
            from app.domain.finance import domain as finance_domain
        except ImportError:
            self.skipTest("当前产物不含 finance 领域（导出电商/通用时正常）")

        self.assertIn(
            "table_query", finance_domain.spec.allowed_tools,
            "财经领域没启用 table_query，表格数字仍然查不到",
        )

    def test_ecommerce_does_not_enable_it(self):
        """电商领域暂不启用（按用户决定，只给财经）。"""
        try:
            from app.domain.ecommerce import domain as ecommerce_domain
        except ImportError:
            self.skipTest("当前产物不含 ecommerce 领域（导出财经时正常）")

        self.assertNotIn(
            "table_query", ecommerce_domain.spec.allowed_tools,
            "电商领域不该有表格查询工具（当前只给财经）",
        )


if __name__ == "__main__":
    unittest.main()
