"""测试辅助：处理"某些测试需要特定领域存在"的情况。

## 解决什么问题

母项目有多个领域（generic / ecommerce / finance），测试里常常这样写：

    d = load_domain("generic")
    self.assertEqual(get_tools_for(d), [])

但**导出产物只有单领域**。产物里跑这些测试会直接报
`ValueError: 未知的领域 'generic'` —— 使用者会以为项目坏了。

## 为什么不在导出脚本里改测试

试过两种做法：

  1. 导出时删掉整个测试文件 —— 会连带丢掉里面**与领域无关**的测试
     （安全、计算器、日期解析），实测会白丢 40 多个用例。

  2. 导出时注入守卫代码 —— 要改脚本、要处理缩进和导入位置，
     复杂且容易出新问题。

  **本方案**：在母项目的测试里就用 `require_domain()` 取领域，
  领域不存在时抛 SkipTest。导出时不需要做任何特殊处理，
  测试文件原样复制过去就能跑。

## 用法

    from tests.domain_guard import require_domain

    def test_generic_has_no_tools(self):
        d = require_domain("generic")      # 产物里会自动跳过
        self.assertEqual(get_tools_for(d), [])
"""

from __future__ import annotations

import unittest

from app.domain import KNOWN_DOMAINS, load_domain


def require_domain(name: str):
    """取一个领域；当前项目里不存在时跳过整个测试。

    Args:
        name: 领域名。

    Returns:
        DomainSpec 实例。

    Raises:
        unittest.SkipTest: 该领域不在本项目里（典型场景：导出后的单领域项目）。
    """
    if name not in KNOWN_DOMAINS:
        raise unittest.SkipTest(
            f"当前项目只包含 {', '.join(KNOWN_DOMAINS)}，"
            f"跳过需要「{name}」领域的测试"
        )
    return load_domain(name)


def has_domain(name: str) -> bool:
    """当前项目是否有某个领域。用于 if 分支里的条件判断。"""
    return name in KNOWN_DOMAINS
