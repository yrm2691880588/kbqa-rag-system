"""领域协议：定义一个「领域」必须提供什么。

## 为什么要有这个文件

Core 需要知道「当前是哪个领域」，才能生成正确的提示词、识别正确的意图。
但它**不能**写死任何具体领域（否则换领域就要改 Core）。

所以这里定义一份**契约**：Core 只依赖这个契约，具体内容由各领域自己填。

## 一个领域要提供什么

| 字段 | 用途 | 例子（电商） | 例子（财经） |
|---|---|---|---|
| `name` | 内部标识 | `ecommerce` | `finance` |
| `display_name` | 展示名 | 电商 | 财经 |
| `persona` | 系统身份 | 你是电商商品知识库问答助手 | 你是财经数据知识库问答助手 |
| `audience` | 回答面向谁 | 普通消费者 | 投资者与从业者 |
| `forbidden` | 合规禁令 | 不编造商品参数 | 不预测涨跌、不推荐买卖 |
| `no_hit_message` | 检索不到资料时的话术 | 没找到相关商品资料 | 没找到相关财经数据 |
| `open_intent_keywords` | 列举意图词 | 有哪些商品 | 有哪些股票 |
| `specific_hints` | 具体对象词（命中即不算列举） | S10、手机、电池 | 招商银行、茅台、市盈率 |
| `samples_dir` | 样例资料目录 | `ecommerce/samples` | `finance/samples` |

## 设计取舍：为什么字段是「文本」而不是「模板占位符」

另一种做法是提供 `prompt_template` 让领域自己写整个提示词。
**没有采用**，因为：

- 提示词里有一批规则是**系统正确性要求**（引用角标必须与片段编号一致、
  不得编造资料之外的内容），不该让领域配置改坏
- 让领域只填「身份 / 受众 / 禁令」这类**语义变量**，
  提示词骨架仍由 Core 控制，能保证所有领域的行为下限一致

这对应决策 Q1：共用规则固定在 Core，领域只提供变化的部分。
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DomainSpec:
    """一个领域的完整描述（不可变）。

    用 frozen=True 的原因：领域配置在启动时确定，运行期不应被修改。
    如果想改领域行为，应该改代码/配置后重启，而不是运行时改对象 ——
    否则会出现「同一个请求内提示词前后不一致」这种难查的问题。
    """

    # ---------- 标识 ----------
    name: str
    """内部标识，与目录名一致（如 `ecommerce`）。用于日志与元数据。"""

    display_name: str
    """展示名（如「电商」）。用于前端标题、导出项目的 README。"""

    # ---------- 提示词语义变量 ----------
    persona: str
    """系统身份。拼进提示词开头，如「你是电商商品知识库问答助手」。"""

    audience: str
    """回答面向的读者。影响措辞与专业术语的解释深度。"""

    forbidden: tuple[str, ...] = ()
    """合规禁令（每条一句话）。

    必须写得具体可执行。反例：「不要违规」——模型无从遵守。
    正例：「不要预测涨跌、不要推荐买卖时机」。
    """

    no_hit_message: str = "在现有知识库中没有找到相关资料"
    """检索不到资料时的提示语。

    必须领域化：电商说「没找到相关商品资料」，财经说「没找到相关财经数据」，
    笼统的「没找到资料」会让用户以为系统坏了。
    """

    # ---------- 意图识别 ----------
    open_intent_keywords: tuple[str, ...] = ()
    """列举意图词：用户表达「想要一份清单」时可能出现的说法。"""

    specific_hints: tuple[str, ...] = ()
    """具体对象词：命中这些词说明用户在问**具体东西**，不应判为列举。

    这一组的存在是为了防误判：
    「介绍一下 S10」里有「介绍一下」（列举词），但 S10 是具体型号，
    应该走 RAG 检索，而不是列清单。
    """

    # ---------- 样例资料 ----------
    samples_subdir: str = "samples"
    """样例资料在领域目录下的子目录名。

    用相对路径而不是绝对路径：导出项目后目录结构会变，
    绝对路径会失效。
    """

    def __post_init__(self) -> None:
        """启动时自检：把「配错了会在运行期才暴露」的问题提前到加载时。

        为什么要自检：
            如果 persona 是空串，提示词会变成「你是。请严格依据……回答」，
            模型行为会很怪，但不会报错 —— 这种问题极难排查。
            在加载时就拦住，比等到用户发现回答变味要好。
        """
        if not self.name or not self.name.isidentifier():
            raise ValueError(f"领域 name 必须是合法的 Python 标识符，实际是 {self.name!r}")
        if not self.persona.strip():
            raise ValueError(f"领域 {self.name} 的 persona（系统身份）不能为空")
        if not self.audience.strip():
            raise ValueError(f"领域 {self.name} 的 audience（受众）不能为空")
        if not self.open_intent_keywords:
            # 不是错误：某些领域可能真的不需要「列举」能力。
            # 但这是个值得注意的信号，所以留一行日志。
            import logging

            logging.getLogger("kbqa.domain").warning(
                "领域 %s 没有配置 open_intent_keywords，" "「有哪些 XX」这类列举问题将不会被识别为开放意图",
                self.name,
            )

    def samples_dir(self) -> pathlib.Path:
        """样例资料目录的绝对路径。

        以本文件所在目录为基准推算，这样即使整个项目被移动位置
        （或导出到别处），路径依然正确。
        """
        return pathlib.Path(__file__).parent / self.name / self.samples_subdir

    @property
    def forbidden_text(self) -> str:
        """把禁令列表拼成提示词里的一段文本。

        为什么要拼成一段编号列表而不是逐条注入：
            模型对「编号列表」的遵守度明显高于散落的句子。
        """
        if not self.forbidden:
            return ""
        lines = [f"{i}. {f}" for i, f in enumerate(self.forbidden, start=1)]
        return "必须遵守以下限制：\n" + "\n".join(lines)
