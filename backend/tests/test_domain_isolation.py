"""跨领域隔离测试（属于 Core）。

## 这个文件要证明什么

领域抽象的目的是「切换领域后，系统只认该领域的词」。
光有各领域的自测（test_open_intent）还不够 —— 那只能证明"自己的词能用"，
不能证明"别人的词不能用"。

本文件专门验证**隔离边界**：在 A 领域下，B 领域的词应当被正确拒绝。

## 为什么这也算 Core 测试

它不假设任何具体领域的存在方式，而是：
  1. 读 KNOWN_DOMAINS 拿到所有已注册领域
  2. 逐个切换，检查各自的词表是否互相干扰

新增领域时这个测试自动覆盖，不需要改代码。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.domain import KNOWN_DOMAINS, load_domain, reset_for_testing
from app.services.open_intent import detect_open_intent


class CrossDomainIsolationTestCase(unittest.TestCase):
    """领域之间的词表不能互相污染。"""

    def setUp(self):
        reset_for_testing()

    def tearDown(self):
        # 还原成测试进程启动时的领域，避免影响后续测试
        os.environ["DOMAIN"] = self._original_domain
        reset_for_testing()

    def setUp(self):  # noqa: F811 - 覆盖上面的空实现
        self._original_domain = os.environ.get("DOMAIN", "generic")
        reset_for_testing()

    def _switch_to(self, domain: str) -> None:
        """切换到指定领域（模拟重启后加载另一个领域）。"""
        os.environ["DOMAIN"] = domain
        reset_for_testing()

    def test_every_known_domain_can_load(self):
        """每个已注册的领域都必须能成功加载。

        为什么重要：新增领域时如果 __init__.py 忘了导出 spec，
        或 domain.py 里有语法错误，只有真正切换过去才会暴露。
        """
        for name in KNOWN_DOMAINS:
            with self.subTest(domain=name):
                spec = load_domain(name)
                self.assertEqual(spec.name, name)
                self.assertTrue(spec.persona, f"{name} 的 persona 不能为空")

    def test_domains_have_distinct_personas(self):
        """不同领域的身份描述不能一模一样。

        如果两个领域的 persona 相同，说明领域抽象没做到位
        （复制粘贴后忘了改），这是很容易犯又很难发现的问题。
        """
        personas = {}
        for name in KNOWN_DOMAINS:
            spec = load_domain(name)
            if spec.persona in personas:
                self.fail(
                    f"领域 {name} 与 {personas[spec.persona]} 的 persona 完全相同："
                    f"{spec.persona!r}。新增领域时记得改身份描述。"
                )
            personas[spec.persona] = name

    def test_foreign_words_blocked_in_each_domain(self):
        """每个领域的 foreign_hints 里配置的词，在该领域下不应被判为列举。

        验证方式：
          切到领域 A，用它自己的列举词开头，接上它配置的 foreign_hint，
          断言结果为 False（说明 foreign_hints 生效了）。
        """
        for name in KNOWN_DOMAINS:
            spec = load_domain(name)
            if not spec.open_intent_keywords or not spec.foreign_hints:
                continue
            self._switch_to(name)

            kw = spec.open_intent_keywords[0]
            foreign = spec.foreign_hints[0]
            with self.subTest(domain=name, keyword=kw, foreign=foreign):
                self.assertFalse(
                    detect_open_intent(f"{kw} {foreign}"),
                    f"领域 {name} 下「{kw} {foreign}」应被 foreign_hints 拦住",
                )

    def test_switching_domain_changes_behavior(self):
        """切换领域后，同一个问题的判断结果应当不同。

        这是隔离生效的最直接证据：
        找一个在 A 领域能命中、在 B 领域不命中的词。
        """
        ecommerce = load_domain("ecommerce")
        finance = load_domain("finance")

        # 电商特有的列举词
        ec_word = "有哪些商品"
        self._switch_to("ecommerce")
        self.assertTrue(
            detect_open_intent(ec_word),
            f"电商领域下 {ec_word!r} 应判为列举",
        )

        self._switch_to("finance")
        self.assertFalse(
            detect_open_intent(ec_word),
            f"财经领域下 {ec_word!r} 不应判为列举（词表应隔离）",
        )

        # 财经特有的列举词，反向验证
        fin_word = "有哪些股票"
        self._switch_to("finance")
        self.assertTrue(
            detect_open_intent(fin_word),
            f"财经领域下 {fin_word!r} 应判为列举",
        )

        self._switch_to("ecommerce")
        self.assertFalse(
            detect_open_intent(fin_word),
            f"电商领域下 {fin_word!r} 不应判为列举（词表应隔离）",
        )


if __name__ == "__main__":
    unittest.main()
