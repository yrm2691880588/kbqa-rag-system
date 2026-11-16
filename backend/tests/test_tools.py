"""工具层测试：计算器安全性、日期解析、注册表、领域配置。

## 重点测什么

**计算器的安全性**是这个文件最重要的部分。

工具参数来自模型，模型输入来自用户。
用户可以通过提示注入让模型生成任意表达式 ——
如果计算器用了 eval，就是任意代码执行漏洞。

所以这里逐个验证各种注入手法都被拒绝。
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.domain import load_domain
from app.tools.calculator import CalculatorTool, safe_eval
from app.tools.date_range import DateRangeTool, parse_period, parse_range
from app.tools.registry import available_tool_names, get_schemas, get_tools_for


class CalculatorSafetyTestCase(unittest.TestCase):
    """计算器绝不能执行任意代码。"""

    def test_normal_arithmetic(self):
        cases = {
            "2+2": 4.0,
            "100-58.7": 41.3,
            "(21.4/20.4-1)*100": 4.90196078431373,
            "1.5*10000": 15000.0,
            "-(3+4)": -7.0,
            "((1+2)*3)": 9.0,
        }
        for expr, expected in cases.items():
            with self.subTest(expr=expr):
                self.assertAlmostEqual(safe_eval(expr), expected, places=8)

    def test_code_injection_rejected(self):
        """各种注入手法都必须被拒绝。"""
        attacks = [
            "__import__('os').system('dir')",
            "eval('1+1')",
            "exec('import os')",
            "open('/etc/passwd')",
            "1+1; import os",
            "a+b",
            "lambda: 1",
            "[1,2,3]",
            "{'a':1}",
            "1 if True else 2",
            "__builtins__",
            "globals()",
        ]
        for expr in attacks:
            with self.subTest(expr=expr):
                with self.assertRaises(ValueError, msg=f"未拦截注入：{expr}"):
                    safe_eval(expr)

    def test_division_by_zero(self):
        with self.assertRaises(ValueError):
            safe_eval("1/0")
        with self.assertRaises(ValueError):
            safe_eval("1/(2-2)")

    def test_too_long_expression(self):
        """超长表达式要拒绝（防拒绝服务）。"""
        with self.assertRaises(ValueError):
            safe_eval("1+" * 1000 + "1")

    def test_empty_expression(self):
        with self.assertRaises(ValueError):
            safe_eval("")
        with self.assertRaises(ValueError):
            safe_eval("   ")

    def test_tool_run_returns_error_not_raise(self):
        """工具执行失败要返回错误文本，不能抛异常。"""
        import asyncio

        t = CalculatorTool()
        r = asyncio.run(t.run(expr="__import__('os')"))
        self.assertFalse(r.ok)
        self.assertIn("不允许", r.text)

    def test_tool_run_normal(self):
        import asyncio

        t = CalculatorTool()
        r = asyncio.run(t.run(expr="2+2"))
        self.assertTrue(r.ok)
        self.assertIn("4", r.text)


class DateRangeTestCase(unittest.TestCase):
    """相对时间解析要准确 —— 财务数据差一年就全错。"""

    def setUp(self):
        self.today = date(2026, 11, 16)

    def test_quarter_parsing(self):
        cases = {
            "去年Q3": (date(2025, 7, 1), date(2025, 9, 30)),
            "今年Q2": (date(2026, 4, 1), date(2026, 6, 30)),
            "2025年Q3": (date(2025, 7, 1), date(2025, 9, 30)),
            "去年第三季度": (date(2025, 7, 1), date(2025, 9, 30)),
        }
        for text, (start, end) in cases.items():
            with self.subTest(text=text):
                p = parse_period(text, self.today)
                self.assertIsNotNone(p, f"解析失败：{text}")
                self.assertEqual(p.start, start)
                self.assertEqual(p.end, end)

    def test_half_year(self):
        p = parse_period("去年下半年", self.today)
        self.assertEqual(p.start, date(2025, 7, 1))
        self.assertEqual(p.end, date(2025, 12, 31))

    def test_full_year(self):
        p = parse_period("2025年全年", self.today)
        self.assertEqual(p.start, date(2025, 1, 1))
        self.assertEqual(p.end, date(2025, 12, 31))

    def test_range_with_shared_year(self):
        """「2025年Q1至Q4」右侧省略年份时要沿用左侧。

        这是实测发现的 bug：不处理的话 Q4 会被解析成"本年的 Q4"。
        """
        r = parse_range("2025年Q1至Q4", self.today)
        self.assertIsNotNone(r)
        p1, p2 = r
        self.assertEqual(p1.start.year, 2025)
        self.assertEqual(p2.start.year, 2025, "右侧没沿用左侧年份")
        self.assertEqual(p2.start.month, 10)  # Q4 从 10 月开始

    def test_range_cross_year(self):
        r = parse_range("去年Q3到今年Q2", self.today)
        self.assertIsNotNone(r)
        p1, p2 = r
        self.assertEqual(p1.start.year, 2025)
        self.assertEqual(p2.start.year, 2026)

    def test_unknown_text_returns_none(self):
        self.assertIsNone(parse_period("随便什么文字", self.today))

    def test_tool_run(self):
        import asyncio

        t = DateRangeTool()
        r = asyncio.run(t.run(text="去年Q3"))
        self.assertTrue(r.ok)
        self.assertIn("2025", r.text)


class RegistryTestCase(unittest.TestCase):
    """注册表按领域筛选工具。"""

    def test_finance_has_both_tools(self):
        d = load_domain("finance")
        names = [t.name for t in get_tools_for(d)]
        self.assertIn("calculator", names)
        self.assertIn("date_range", names)

    def test_generic_has_no_tools(self):
        """通用领域不该启用任何工具（不增加开销）。"""
        d = load_domain("generic")
        self.assertEqual(get_tools_for(d), [])

    def test_unknown_tool_name_skipped(self):
        """配置写了不存在的工具名，跳过而不是崩。"""
        from app.domain.base import DomainSpec

        spec = DomainSpec(
            name="tmp_test", display_name="测试",
            persona="你是测试助手", audience="测试者",
            allowed_tools=("calculator", "not_exists_tool"),
        )
        tools = get_tools_for(spec)
        self.assertEqual([t.name for t in tools], ["calculator"])

    def test_schemas_format(self):
        d = load_domain("finance")
        schemas = get_schemas(get_tools_for(d))
        self.assertTrue(schemas)
        for s in schemas:
            self.assertEqual(s["type"], "function")
            self.assertIn("name", s["function"])
            self.assertIn("parameters", s["function"])

    def test_available_names(self):
        names = available_tool_names()
        self.assertIn("calculator", names)
        self.assertIn("date_range", names)


class ExecuteToolTestCase(unittest.TestCase):
    """执行工具时的边界情况。"""

    def test_unknown_tool_returns_error(self):
        import asyncio

        from app.tools.registry import execute_tool

        tools = get_tools_for(load_domain("finance"))
        r = asyncio.run(execute_tool(tools, "not_exists", {}))
        self.assertIn("不可用", r)

    def test_tool_not_in_domain_rejected(self):
        """领域没启用的工具，即使模型"猜"到名字也不能调。"""
        import asyncio

        from app.tools.registry import execute_tool

        # generic 领域没有工具
        tools = get_tools_for(load_domain("generic"))
        r = asyncio.run(execute_tool(tools, "calculator", {"expr": "2+2"}))
        self.assertIn("不可用", r)

    def test_bad_params_returns_error(self):
        import asyncio

        from app.tools.registry import execute_tool

        tools = get_tools_for(load_domain("finance"))
        # calculator 需要 expr，这里给个错的字段名
        r = asyncio.run(execute_tool(tools, "calculator", {"wrong": "2+2"}))
        # 应该返回错误说明而不是抛异常
        self.assertIsInstance(r, str)
        self.assertTrue(r)


class ToolDescriptionTestCase(unittest.TestCase):
    """工具描述本身也是被测对象。

    ## 为什么描述值得单独测

    工具描述是模型决策的**唯一依据**。它写得好不好，
    直接决定"该调用时会不会调用"。

    实测踩过两个坑：
      1. date_range 的描述里 `{today}` 没被替换（转义写错），
         模型看到的字面就是 `{today}`，等于没告诉它今天几号。
      2. calculator 的描述原来写"只在确实需要时使用"、
         "如果资料里有就不要调用" —— 太保守，模型对
         「Q3 比 Q2 增长多少」也不调用，而是抄资料里现成的比率。
    """

    def test_date_range_description_has_real_date(self):
        """描述里必须出现具体日期，不能是 {today} 这种占位符。"""
        d = DateRangeTool()
        desc = d.description

        self.assertNotIn(
            "{today}", desc,
            "描述里还有未替换的 {today} 占位符 —— 模型不知道今天是几号",
        )
        # 应该含一个形如 2026-11-16 的真实日期
        import re

        self.assertRegex(
            desc, r"\d{4}-\d{2}-\d{2}",
            "描述里没有具体日期",
        )

    def test_calculator_description_has_explicit_trigger(self):
        """描述里要有明确的"什么时候必须调用"的表述。"""
        d = CalculatorTool()
        desc = d.description

        self.assertIn("必须调用", desc, "缺少'必须调用'的明确要求")
        self.assertIn("增长率", desc, "没提到增长率的场景")
        self.assertIn("不要调用", desc, "缺少'什么时候不要调用'的说明")

    def test_calculator_description_not_over_conservative(self):
        """描述不能是"能不调就不调"的措辞。

        实测发现"只在你确实需要时使用"这种写法会让模型过度保守。
        """
        d = CalculatorTool()
        desc = d.description

        # 不应该出现纯劝退的措辞
        for bad in ("只有在万不得已", "尽量不要调用", "非必要不调用"):
            self.assertNotIn(bad, desc, f"描述过于保守：{bad}")

    def test_all_tools_have_description(self):
        """每个工具都必须有非空描述，否则模型不会用。"""
        from app.tools.calculator import CalculatorTool as CT
        from app.tools.date_range import DateRangeTool as DT

        for tool in (CT(), DT()):
            self.assertTrue(tool.description, f"{tool.name} 缺少描述")
            self.assertGreater(
                len(tool.description), 20,
                f"{tool.name} 的描述太短，模型难以判断何时使用",
            )

    def test_all_tools_have_schema(self):
        """每个工具必须有合法 JSON Schema。"""
        from app.tools.calculator import CalculatorTool as CT
        from app.tools.date_range import DateRangeTool as DT

        for tool in (CT(), DT()):
            p = tool.parameters
            self.assertEqual(p.get("type"), "object")
            self.assertIn("properties", p)
            self.assertTrue(p.get("required"), f"{tool.name} 没有必填参数")


if __name__ == "__main__":
    unittest.main()
