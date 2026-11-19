"""工具接口：所有工具都实现它。

## 为什么先定接口再写工具

用户担心"现在手写、以后用框架会不会冲突"。
冲突的根源不是"用不用框架"，而是**有没有统一的形状**。

只要每个工具都是「名字 + 描述 + 参数 schema + 执行函数」，
那么：
  · 现在手写的工具
  · 以后用框架包装的工具
  · 以后接 MCP 的工具
对上层来说长得一样，加工具只是加一个实现，不动核心代码。

## 这个接口为什么只有四个成员

对照 OpenAI / Anthropic / MCP 三家的工具定义，共同的字段只有这几个：
    name / description / parameters(schema) / execute

多一个字段就多一处要维护。宁可少，也不要为"可能用到"预留。
真需要扩展时（比如成本估算、权限标记），加可选属性即可。

## 安全约定（写进接口注释，避免以后加工具时忘了）

1. **工具参数来自模型，模型输入来自用户** ——
   参数是不可信输入，必须校验，不能用 eval 之类的东西。
2. **当前所有工具都是纯函数** —— 不碰数据库、不碰文件、不联网。
   以后加"有副作用"的工具时必须单独审查。
3. **执行失败不要抛异常给上层** —— 返回错误文本让模型知道"这次没成功"，
   它会用现有信息继续组织答案。抛异常会让整轮问答挂掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。

    为什么不用裸字符串：
        自检需要知道"这个结果是谁算的、用了什么输入"，
        否则无法核对"用的数对不对"。
        结构化一点，后面有用。
    """

    text: str
    """给模型看的结果文本。"""

    ok: bool = True
    """是否成功。失败时 text 里应包含错误说明。"""

    meta: dict[str, Any] = field(default_factory=dict)
    """附加信息（如算式、输入值），用于日志与自检追溯。"""


@runtime_checkable
class Tool(Protocol):
    """工具接口。

    实现者只需要提供这四个成员。
    """

    name: str
    """工具名。模型通过这个名字调用。

    要求：小写字母+下划线，语义明确（如 `calculator` 而非 `calc`）。
    """

    description: str
    """用途说明。**模型据此决定要不要调用**。

    写法要求（实测总结）：
      · 说清"什么时候该用"比说清"它是什么"更重要
      · 例：「当需要做算术计算（增长率、占比、单位换算）时使用。
             不需要算数时不要调用。」
      · 不写清楚会导致模型对简单问题也调用工具，白费一次往返。
    """

    parameters: dict[str, Any]
    """参数的 JSON Schema。

    形如：
        {
            "type": "object",
            "properties": {"expr": {"type": "string", "description": "..."}},
            "required": ["expr"],
        }
    """

    async def run(self, **kwargs: Any) -> ToolResult:
        """执行工具。

        Args:
            **kwargs: 由模型按 parameters 填写的参数。

        Returns:
            ToolResult。**失败不要抛异常**，返回 ok=False 与错误说明。

        Note:
            必须遵守 base.py 顶部列出的三条安全约定。
        """
        ...


def to_openai_schema(tool: Tool) -> dict[str, Any]:
    """把工具转成 OpenAI tools 参数需要的格式。

    单独成函数而不是让每个工具自己提供：
    格式细节（type/function 嵌套）是**接口约定**，不该让每个工具重复实现。
    以后换供应商时只改这里。
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }
