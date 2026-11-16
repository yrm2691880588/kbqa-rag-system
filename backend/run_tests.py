"""测试入口：一次跑完「Core 通用测试 + 指定领域的专属测试」。

## 为什么需要它

`python -m unittest discover -s tests` 只会发现 `tests/` 目录下的文件，
**发现不到** `app/domain/<name>/tests/` 里的领域测试 ——
那意味着领域测试写了却没人跑，形同虚设。

本脚本把两处都纳入：
  1. `tests/`                          ← Core 通用测试
  2. `app/domain/<DOMAIN>/tests/`      ← 当前领域的专属测试

## 用法

    # 跑 Core + 电商领域测试
    DOMAIN=ecommerce python run_tests.py

    # 只列会跑哪些包，不真的执行
    python run_tests.py --list

## 为什么不直接用 pytest

本项目从第一天就用标准库 `unittest`（requirements.txt 里没有 pytest），
保持一致，不额外引入依赖。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))


def _domain_name() -> str:
    """当前要测的领域。未设置时用 generic（与生产逻辑一致）。"""
    return (os.environ.get("DOMAIN") or "generic").strip().lower()


def _collect_domain_tests(loader: unittest.TestLoader, domain: str) -> unittest.TestSuite:
    """加载某个领域的专属测试。

    领域目录不存在、或没有 tests 子包时返回空 suite（不是错误）——
    有些领域可能暂时没有专属测试。
    """
    tests_dir = HERE / "app" / "domain" / domain / "tests"
    if not tests_dir.is_dir():
        return unittest.TestSuite()
    try:
        # 用 discover 而不是 loadTestsFromName：
        #   loadTestsFromName("app.domain.x.tests") 只会加载**包本身**，
        #   不会自动递归到包内的 test_*.py 模块，结果是 0 个用例却"成功"了。
        #   这是静默失效，必须避开。
        return loader.discover(
            str(tests_dir),
            pattern="test_*.py",
            top_level_dir=str(HERE),
        )
    except Exception as exc:  # noqa: BLE001 - 领域测试导入失败要明确报出来，不能静默跳过
        print(f"[警告] 领域 {domain} 的测试加载失败：{exc}", file=sys.stderr)
        return unittest.TestSuite()


def main() -> int:
    domain = _domain_name()
    os.environ["DOMAIN"] = domain  # 领域测试里去读 get_domain() 时要用

    loader = unittest.TestLoader()
    core = loader.discover(str(HERE / "tests"), top_level_dir=str(HERE))
    dom = _collect_domain_tests(loader, domain)

    if "--list" in sys.argv:
        print(f"DOMAIN = {domain}")
        print(f"Core 测试      : {core.countTestCases()} 个")
        print(f"领域专属测试   : {dom.countTestCases()} 个")
        return 0

    suite = unittest.TestSuite([core, dom])
    total = suite.countTestCases()
    print(f"=== 领域：{domain} | 共 {total} 个用例 ===", flush=True)

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
