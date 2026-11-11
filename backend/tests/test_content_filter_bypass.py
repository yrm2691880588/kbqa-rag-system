"""内容过滤的绕过防护与分类名回归测试。

这个文件在测什么（大白话）：
    内容过滤是"唯一会误伤正常用户"的模块 —— 误拦比漏拦更伤体验。
    但它同时又必须真的拦得住，否则等于没做。
    这两件事互相拉扯，所以每一条规则都要用测试钉死。

本文件覆盖两轮 code review 发现的真实漏洞：
    ① 零宽字符绕过：往词中间塞不可见字符，之前能整句绕过审核
    ② 分类名泄露：命中时把词表里的 "=== 分隔线 ===" 和说明文字
       当成分类名回显给用户，既难看又泄露内部结构
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import content_filter as cf


class ZeroWidthBypassTestCase(unittest.TestCase):
    """不可见字符不能成为绕过审核的手段。

    背景：实测「赌\\u200b博」「赌\\ufeff博」「加😀qq」原本全部 hit=False。
    根因是 _SKIP_CHARS 靠人工列举，覆盖不到 Unicode 里上百个格式字符。
    现在改成按 Unicode 类别过滤（Cf/Mn/Me/So/Sk/Cc）。
    """

    def test_zero_width_space_cannot_bypass(self):
        self.assertTrue(cf.check("赌\u200b博").hit, "零宽空格能绕过过滤")

    def test_bom_cannot_bypass(self):
        self.assertTrue(cf.check("赌\ufeff博").hit, "BOM 能绕过过滤")

    def test_zero_width_joiner_cannot_bypass(self):
        self.assertTrue(cf.check("加\u200dqq").hit, "零宽连接符能绕过过滤")

    def test_soft_hyphen_cannot_bypass(self):
        self.assertTrue(cf.check("赌\u00ad博").hit, "软连字符能绕过过滤")

    def test_word_joiner_cannot_bypass(self):
        self.assertTrue(cf.check("加\u2060qq").hit, "词连接符能绕过过滤")

    def test_emoji_cannot_bypass(self):
        self.assertTrue(cf.check("加😀qq").hit, "emoji 能绕过过滤")

    def test_multiple_invisible_chars_cannot_bypass(self):
        mixed = "赌\u200b\u200c\u200d\ufeff博"
        self.assertTrue(cf.check(mixed).hit, "多个不可见字符叠加能绕过过滤")

    def test_normal_text_still_passes(self):
        """去掉不可见字符的能力不能变成"什么都能拦"。"""
        for text in (
            "Watch2 的电池续航怎么样？",
            "这款手机多少钱",
            "📱 屏幕多大？",          # 正常问题里带 emoji 不该被拦
            "你好",
            "支持 5G 吗",
        ):
            r = cf.check(text)
            self.assertFalse(r.hit, f"正常问题被误拦：{text!r} -> {[h.word for h in r.hits]}")

    def test_spaced_and_symbol_separated_still_blocked(self):
        """原有能力不能回退：插空格、插符号同样要拦得住。"""
        for text in ("赌 博", "赌-博", "赌*博", "赌  博"):
            self.assertTrue(cf.check(text).hit, f"{text!r} 没拦住")

    def test_traditional_chinese_still_blocked(self):
        """原有能力不能回退：繁体写法要拦得住。"""
        self.assertTrue(cf.check("賭博").hit, "繁体没拦住")


class CategoryLeakTestCase(unittest.TestCase):
    """命中时返回的分类名必须是干净的业务分类，不能是词表里的注释。

    背景：早期把所有 # 开头的行都当分类名，
    于是命中「赌博」返回的分类名是
      "==================== 违法交易与诈骗 ===================="
    而这个分类名会被 chat.py 回显给提问者 —— 既难看又泄露内部结构。
    """

    def test_category_has_no_separator_symbols(self):
        r = cf.check("赌博")
        self.assertTrue(r.hit)
        for h in r.hits:
            self.assertNotIn("=", h.category, f"分类名含分隔符：{h.category!r}")
            self.assertNotIn("-", h.category, f"分类名含分隔符：{h.category!r}")

    def test_category_is_reasonable_length(self):
        """分类名应该是一句话/一个短语，而不是整段说明。"""
        wl = cf.load_wordlist(force=True)
        for cat in set(wl.categories):
            self.assertLess(len(cat), 20, f"分类名过长（疑似说明文字被当成分类）：{cat!r}")

    def test_category_is_not_instruction_text(self):
        """不能把"怎么用""注意：""说明："这类说明文字当分类名。"""
        wl = cf.load_wordlist(force=True)
        for cat in set(wl.categories):
            for bad in ("怎么用", "注意", "说明", "生效范围", "大小写"):
                self.assertNotIn(
                    bad, cat, f"分类名里混进了说明文字：{cat!r}"
                )

    def test_known_categories_are_clean(self):
        r = cf.check("加微信")
        self.assertTrue(r.hit)
        cats = {h.category for h in r.hits}
        self.assertIn("广告与导流", cats, f"实际拿到：{cats}")


if __name__ == "__main__":
    unittest.main()
