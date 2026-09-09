"""闲聊识别模块单元测试（smalltalk.py）——验证寒暄识别是否可靠、不误伤真问题。

运行方式（在 backend 目录下）：
    C:\\Users\\pjs\\.venvs\\kbqa\\Scripts\\python.exe -m unittest tests.test_smalltalk -v
"""
import unittest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.smalltalk import (
    detect_smalltalk,
    _MSG_GREET,
    _MSG_THANKS,
    _MSG_BYE,
    _MSG_WHOAMI,
)


class TestDetectSmalltalk(unittest.TestCase):
    """detect_smalltalk(question) -> str | None 的测试组。

    返回值语义：命中寒暄返回对应礼貌文案（表示"拦截、直接回复"）；
    返回 None 表示"不是闲聊，放行进知识库检索"。
    """

    # ---------- 正常用例：各类纯寒暄都应被拦截 ----------

    def test_纯问候被拦截(self):
        """'你好' 是最常见的问候，应命中问候文案。"""
        self.assertEqual(detect_smalltalk("你好"), _MSG_GREET)

    def test_您好加语气词被拦截(self):
        """'您好呀' 剥掉'您好'后只剩语气词'呀'，应命中问候。"""
        self.assertEqual(detect_smalltalk("您好呀"), _MSG_GREET)

    def test_英文问候被拦截(self):
        """大小写混写的英文 hi/Hello 应命中问候。"""
        self.assertEqual(detect_smalltalk("hi"), _MSG_GREET)
        self.assertEqual(detect_smalltalk("Hello"), _MSG_GREET)

    def test_在吗被拦截(self):
        """'在吗' 是典型寒暄开场，应命中问候。"""
        self.assertEqual(detect_smalltalk("在吗"), _MSG_GREET)

    def test_致谢被拦截(self):
        """'谢谢' / '辛苦了' 应命中致谢文案。"""
        self.assertEqual(detect_smalltalk("谢谢"), _MSG_THANKS)
        self.assertEqual(detect_smalltalk("辛苦了"), _MSG_THANKS)

    def test_告别被拦截(self):
        """'再见' / '拜拜' 应命中告别文案。"""
        self.assertEqual(detect_smalltalk("再见"), _MSG_BYE)
        self.assertEqual(detect_smalltalk("拜拜"), _MSG_BYE)

    # ---------- 边界用例 ----------

    def test_身份询问命中身份文案(self):
        """'你是谁''你能做什么' 属自我介绍类，应回身份文案而非普通问候。"""
        self.assertEqual(detect_smalltalk("你是谁"), _MSG_WHOAMI)
        self.assertEqual(detect_smalltalk("你能做什么"), _MSG_WHOAMI)
        self.assertEqual(detect_smalltalk("介绍一下你自己"), _MSG_WHOAMI)

    def test_空输入返回None(self):
        """None / 空串 / 纯空白都不该崩溃，应视为非闲聊放行。"""
        self.assertIsNone(detect_smalltalk(""))
        self.assertIsNone(detect_smalltalk("   "))
        self.assertIsNone(detect_smalltalk(None))

    def test_纯标点返回None(self):
        """只有标点符号没有实质内容，不算寒暄（避免误拦截）。"""
        self.assertIsNone(detect_smalltalk("，，，"))

    def test_寒暄夹带真实问题放行(self):
        """'你好,A8多少钱' 去掉'你好'后还剩实质问题 → 必须返回 None 放行进 RAG。"""
        self.assertIsNone(detect_smalltalk("你好,A8净化器多少钱"))
        self.assertIsNone(detect_smalltalk("在吗,手表能防水吗"))
        self.assertIsNone(detect_smalltalk("谢谢,那A8的滤芯怎么换"))

    def test_寒暄词出现在句中被误伤防护(self):
        """'星耀A8适合多大面积' 不含寒暄词 → 放行。"""
        self.assertIsNone(detect_smalltalk("星耀A8适合多大面积"))

    def test_产品功能询问不被误伤(self):
        """回归：产品功能类问题（含'有什么功能/能做什么/介绍一下'）必须放行进 RAG，
        不能被当成'你是谁'之类的身份闲聊拦截。对应真实 bug：smalltalk 身份词表过宽。"""
        for q in [
            "净化器有什么功能",
            "净化器有没有什么功能",
            "星耀A8有什么功能",
            "介绍一下星耀A8",
            "介绍下星翼蓝牙耳机",
            "S10能做什么智能操作",
            "净化器能做什么",
            "你能做什么产品评测",
            "这手机有什么功能特点",
        ]:
            self.assertIsNone(detect_smalltalk(q), f"产品问题被误伤拦截: {q}")

    def test_身份询问必须拦截(self):
        """回归：真正的身份/能力询问应返回身份文案，不应放行漏拦。"""
        for q in ["你是谁", "你能做什么", "你有什么功能", "介绍一下你自己", "自我介绍一下", "你都会做什么", "你会什么", "你叫什么名字"]:
            self.assertEqual(detect_smalltalk(q), _MSG_WHOAMI, f"身份询问漏拦: {q}")

    def test_重复调用结果稳定(self):
        """同一输入调用两次结果一致（无累积副作用）。"""
        self.assertEqual(detect_smalltalk("你好"), detect_smalltalk("你好"))


if __name__ == "__main__":
    unittest.main()
