"""工具注册表：按领域筛选可用的工具。

## 这个模块解决什么

工具写好了，但「哪个领域能用哪些工具」需要一处统一管理：

    finance   → calculator + date_range + table_query
    ecommerce → calculator
    generic   → （空）

## 为什么用"注册 + 按领域筛选"而不是"直接 import 具体工具"

1. **解耦**：rag.py 不需要知道有哪些工具，只问注册表要
2. **领域化**：领域配置只写工具名字符串，不 import 类
   —— 这样领域模块不会被工具的实现细节污染
3. **可关闭**：配置里去掉名字就等于关掉，不用改代码

## 为什么工具名用字符串而不是枚举

领域配置是**声明式**的（和 display_name、persona 一样）。
用字符串能让领域配置保持纯数据，新增工具时不用改公共枚举。
写错名字的情况由 `get_tools_for` 报警并跳过，不会崩。
"""

from __future__ import annotations

import logging

from app.domain.base import DomainSpec
from app.tools.base import Tool, to_openai_schema
from app.tools.calculator import CalculatorTool
from app.tools.date_range import DateRangeTool
from app.tools.table_query import TableQueryTool

logger = logging.getLogger("kbqa.tools.registry")


# 内置工具表：名字 -> 实例。
#
# 新增工具只需在这里加一行 + 在领域配置里加名字。
# 不需要改 rag.py、不需要改注册逻辑。
_BUILTIN: dict[str, Tool] = {
    "calculator": CalculatorTool(),
    "date_range": DateRangeTool(),
    "table_query": TableQueryTool(),
}


def available_tool_names() -> tuple[str, ...]:
    """所有已注册的工具名。供配置校验与文档使用。"""
    return tuple(_BUILTIN.keys())


def get_tools_for(domain: DomainSpec) -> list[Tool]:
    """取出某个领域可用的工具实例。

    Args:
        domain: 领域配置。

    Returns:
        工具实例列表。领域没配工具时返回空列表（调用方据此完全跳过多余逻辑）。

    Note:
        配置里写了未注册的工具名时**打 warning 并跳过**，不抛异常 ——
        写错一个名字不该让整个问答挂掉。
    """
    names = getattr(domain, "allowed_tools", ()) or ()
    if not names:
        return []

    tools: list[Tool] = []
    for name in names:
        tool = _BUILTIN.get(name)
        if tool is None:
            logger.warning(
                "领域 %s 配置了未注册的工具 %r，已跳过（可用：%s）",
                domain.name, name, ", ".join(_BUILTIN),
            )
            continue
        tools.append(tool)
    return tools


def get_schemas(tools: list[Tool]) -> list[dict]:
    """把工具实例转成 API 需要的 schema 列表。"""
    return [to_openai_schema(t) for t in tools]


async def execute_tool(
    tools: list[Tool],
    name: str,
    arguments: dict,
    context: dict | None = None,
) -> str:
    """按名字执行一个工具，返回给模型看的结果文本。

    Args:
        tools: 当前可用的工具列表（**只在这个列表里找**，
               防止模型调用领域未启用的工具）。
        name: 工具名。
        arguments: 模型填的参数。
        context: 调用方提供的上下文（如"当前知识库有哪些表格文件"）。
            **只有声明了 needs_context 的工具才会收到它**。

            为什么不无条件传：大多数工具是纯函数（计算器、日期），
            多传一个用不上的参数会让"纯函数"这个约定变得模糊。
            显式标记 + 按需注入，意图更清楚。

    Returns:
        结果文本。任何失败都返回错误说明，不抛异常 ——
        工具失败不该让整轮问答挂掉，模型可以据此改用别的信息作答。
    """
    # 只在允许的列表里找：模型可能"猜"出一个未启用的工具名
    target = next((t for t in tools if t.name == name), None)
    if target is None:
        return f"错误：工具 {name} 不可用"

    try:
        if getattr(target, "needs_context", False) and context:
            # 用下划线前缀传，避免和模型能填的参数名撞车
            result = await target.run(_context=context, **arguments)
        else:
            result = await target.run(**arguments)
        return result.text
    except TypeError as exc:
        # 参数不匹配（模型填错了字段名）
        logger.warning("工具 %s 参数不匹配：%s", name, exc)
        return f"错误：工具 {name} 的参数不正确（{exc}）"
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具 %s 执行失败：%s", name, exc)
        return f"错误：工具 {name} 执行失败"
