"""开放性意图检测模块单元测试（open_intent.py）。

被测函数：detect_open_intent(question) -> bool
判定为 True 时，RAG 主流程会在无命中情况下主动列出商品清单。

运行方式（在 backend 目录下）：
    python -m unittest tests.test_open_intent -v
"""
import unittest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.open_intent import detect_open_intent, explain


class TestDetectOpenIntent(unittest.TestCase):
    """detect_open_intent(question) -> bool 的测试组。

    设计边界：开放性意图（列商品/总览）与具体问题（问某商品某属性）必须正确分流。
    """

    # ---------- 正常用例：明确列举意图应命中 ----------

    def test_用户原话_请介绍一下本商品信息(self):
        """用户截图中的实际输入。"""
        self.assertTrue(detect_open_intent("你好,请介绍一下本商品信息"))

    def test_裸介绍一下(self):
        """只说'介绍一下' → 应判定为开放性（要列商品）。"""
        self.assertTrue(detect_open_intent("介绍一下"))

    def test_请介绍一下商品(self):
        self.assertTrue(detect_open_intent("请介绍一下商品"))

    def test_你们有什么商品(self):
        self.assertTrue(detect_open_intent("你们有什么商品"))

    def test_你们卖什么(self):
        self.assertTrue(detect_open_intent("你们卖什么"))

    # ---------- 边界用例：含具体型号/品类/属性 → 不算开放性 ----------

    def test_含型号S10不算开放(self):
        """用户问'S10 电池容量'——有具体型号，不应触发列商品。"""
        self.assertFalse(detect_open_intent("S10电池容量是多少"))

    def test_含品类手机不算开放(self):
        """'手机有什么快充功能'——含品类词'手机'+属性'快充'，应交给具体问答。"""
        self.assertFalse(detect_open_intent("手机有什么快充功能"))

    def test_含品类耳机不算开放(self):
        """'耳机防水等级'——具体问题。"""
        self.assertFalse(detect_open_intent("耳机的防水等级"))

    def test_具体产品名不算开放(self):
        """'星耀A8适用多大面积'——有具体产品。"""
        self.assertFalse(detect_open_intent("星耀A8适用多大面积"))

    def test_型号Watch2不算开放(self):
        self.assertFalse(detect_open_intent("Watch2 防水吗"))

    # ---------- 异常/边界用例 ----------

    def test_空输入返回False(self):
        """空串/纯空白不应触发。"""
        self.assertFalse(detect_open_intent(""))
        self.assertFalse(detect_open_intent("   "))
        self.assertFalse(detect_open_intent(None))

    def test_纯问候不触发(self):
        """'你好' 不含列举词，应判具体问题（即交给寒暄识别）。"""
        self.assertFalse(detect_open_intent("你好"))

    def test_具体问题加问号(self):
        """问号/感叹号不影响判定。"""
        self.assertFalse(detect_open_intent("S10 多少钱？"))

    def test_大写英文也走归一化(self):
        """大小写不敏感。"""
        # 'WHAT DO YOU SELL' 命中 '你们卖什么' 不应误中（没命中关键词,直接 False）
        self.assertFalse(detect_open_intent("WHAT DO YOU SELL"))

    def test_混合具体与总览(self):
        """'你们有星耀A8吗' 含列举词'你们有'，但含具体型号'A8' → 应判具体。"""
        self.assertFalse(detect_open_intent("星耀A8你们有吗"))

    # ---------- 幂等/可重复 ----------

    def test_重复调用结果稳定(self):
        for _ in range(3):
            self.assertEqual(detect_open_intent("介绍一下本商品信息"),
                             detect_open_intent("介绍一下本商品信息"))


class TestExplain(unittest.TestCase):
    """explain() 诊断信息测试。"""

    def test_explain命中关键词(self):
        info = explain("介绍一下本商品信息")
        self.assertTrue(info["is_open"])
        self.assertEqual(info["matched_keyword"], "介绍一下本商品")
        self.assertEqual(info["blocked_by_hint"], "")

    def test_explain被具体词挡住(self):
        """'你们有什么手机推荐'：命中列举词'你们有什么'，但被'手机'拦住。"""
        info = explain("你们有什么手机推荐")
        self.assertFalse(info["is_open"])
        self.assertEqual(info["matched_keyword"], "你们有什么")
        self.assertEqual(info["blocked_by_hint"], "手机")


if __name__ == "__main__":
    unittest.main()
