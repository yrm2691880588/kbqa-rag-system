"""领域模块包。

## 这是什么

系统支持多种业务领域（电商、财经、医疗……）。每个领域的所有**专属内容**
放在本包下的一个子目录里，Core（通用引擎）不含任何领域字样。

## 目录约定

```
app/domain/
├── __init__.py          ← 本文件：领域加载入口
├── base.py              ← 领域协议定义（所有领域必须实现什么）
├── ecommerce/           ← 电商领域
│   ├── __init__.py
│   ├── domain.py        ← 实现 DomainSpec
│   ├── samples/         ← 样例资料
│   └── tests/           ← 领域专属测试
├── finance/             ← 财经领域
└── generic/             ← 兜底领域（未指定 DOMAIN 时使用）
```

## 加载规则

- 启动时通过环境变量 `DOMAIN` 指定要加载哪个领域
- **一次只加载一个**：未选中的领域代码留在磁盘，但不被导入
- 未指定时回退到 `generic`；`APP_ENV=prod` 时强制要求显式指定

## 依赖方向（重要）

```
domain/*  ──依赖──▶  core/, services/, models/
core/     ──✗ 不得依赖 ──▶  domain/*
```

Core 里出现任何领域专有名词（商品、股票、诊断……）都算违规 ——
那意味着换领域时要改 Core，与本架构的目的相反。
"""

from __future__ import annotations

import importlib
import logging

from app.domain.base import DomainSpec

logger = logging.getLogger("kbqa.domain")

# 已知的领域名。新增领域时在这里登记，导出脚本据此校验。
KNOWN_DOMAINS: tuple[str, ...] = ("ecommerce", "finance", "generic")

# 兜底领域：未指定 DOMAIN 时使用
DEFAULT_DOMAIN = "generic"

# 模块级缓存：启动后不再变化，避免每次请求都做 import 查找
_loaded: DomainSpec | None = None


def load_domain(name: str) -> DomainSpec:
    """加载指定领域模块并返回其实例。

    Args:
        name: 领域名，必须是 KNOWN_DOMAINS 之一。

    Returns:
        DomainSpec 实例。

    Raises:
        ValueError: 领域名未知，或该领域模块没有提供 `spec` 对象。
        ImportError: 模块存在但导入失败（通常是模块内部写错了）。
    """
    if name not in KNOWN_DOMAINS:
        raise ValueError(
            f"未知的领域 {name!r}。可选值：{', '.join(KNOWN_DOMAINS)}。"
            f"新增领域后请同步更新 app/domain/__init__.py 的 KNOWN_DOMAINS。"
        )

    module = importlib.import_module(f"app.domain.{name}")
    spec = getattr(module, "spec", None)
    if spec is None:
        raise ValueError(
            f"领域模块 app.domain.{name} 没有提供 `spec` 对象。"
            f"每个领域的 __init__.py 必须导出一个 DomainSpec 实例命名为 spec。"
        )
    logger.info("已加载领域：%s（%s）", spec.name, spec.display_name)
    return spec


def get_domain() -> DomainSpec:
    """取当前已加载的领域（供 Core 调用）。

    首次调用时按 `settings.domain` 加载并缓存；之后直接返回缓存。
    这样 Core 里的代码只需 `get_domain().persona`，不必关心加载细节。
    """
    global _loaded
    if _loaded is None:
        from app.core.config import settings

        _loaded = load_domain(settings.domain)
    return _loaded


def reset_for_testing() -> None:
    """清空缓存（仅测试用）。

    测试里可能需要在同一个进程内切换领域，没有这个钩子就做不到
    —— 因为 `_loaded` 一旦被赋值就再也不会重新加载。
    """
    global _loaded
    _loaded = None
