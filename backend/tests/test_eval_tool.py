"""双层评估工具本身的测试。

## 为什么评估工具也要测

评估工具若本身算错，会导致：
  · 误判"系统变差了"去改本来没问题的代码
  · 误判"系统变好了"放过了回归

比没评估更糟 —— 因为它给出的是**看似可信的数字**。

## 这轮实测暴露的两个坑

1. **Windows 编码崩溃**：脚本 print emoji 直接抛 UnicodeEncodeError，
   整个工具跑不起来。已修（强制 UTF-8 输出）。

2. **配置项名猜错**：写成 settings.rerank_enabled，但该字段不存在
   （实际靠 rerank_model + rerank_api_key 判断）。已修。
"""

import json
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class KeywordHitTestCase(unittest.TestCase):
    """答案层的关键词匹配逻辑。"""

    def setUp(self):
        # eval_all.py 在 scripts 目录下，不在包内，按路径导入
        sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
        from eval_all import keyword_hit

        self.keyword_hit = staticmethod(keyword_hit)

    def test_all_keywords_present(self):
        ok, missing = self.keyword_hit("利润为 142.7 万元", ["142.7"])
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_keyword_missing(self):
        ok, missing = self.keyword_hit("资料中没有披露该数据", ["142.7"])
        self.assertFalse(ok)
        self.assertEqual(missing, ["142.7"])

    def test_multiple_keywords_partial(self):
        """多个关键词只命中一部分时，要报出缺哪些。"""
        ok, missing = self.keyword_hit("答案是 142.7", ["142.7", "386.2"])
        self.assertFalse(ok)
        self.assertEqual(missing, ["386.2"])

    def test_empty_keywords_always_pass(self):
        """没填关键词 = 只测检索层，答案层视为通过。"""
        ok, missing = self.keyword_hit("随便什么答案", [])
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_empty_answer_fails(self):
        ok, _ = self.keyword_hit("", ["142.7"])
        self.assertFalse(ok, "空答案不可能包含关键词")


class EvalSetFormatTestCase(unittest.TestCase):
    """评估集格式的完整性检查。"""

    def setUp(self):
        # 按当前领域找对应的评估集。
        #
        # 为什么不写死 finance：导出电商/通用领域时，
        # `backend/eval/` 整个目录会被排除（里面全是财经内容和期望答案，
        # 带过去纯属污染）。此时这个测试应该跳过，而不是失败。
        import os

        domain = os.environ.get("DOMAIN", "generic").strip() or "generic"
        self.path = (
            pathlib.Path(__file__).parent.parent / "eval" / f"eval_set.{domain}.json"
        )

    def _load(self):
        """读评估集；不存在就跳过这个测试。"""
        if not self.path.exists():
            self.skipTest(
                f"当前领域 {self.path.stem} 没有评估集（导出产物里这是正常的）"
            )
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_eval_set_exists(self):
        if not self.path.exists():
            self.skipTest(
                f"当前领域没有评估集 {self.path.name}（导出产物里这是正常的）"
            )
        self.assertTrue(self.path.exists())

    def test_eval_set_is_valid_json(self):
        data = self._load()
        self.assertIn("cases", data)
        self.assertGreater(len(data["cases"]), 0)

    def test_each_case_has_required_fields(self):
        """每个 case 必须有 question 和 expect_docs。"""
        data = self._load()
        for i, c in enumerate(data["cases"]):
            with self.subTest(index=i):
                self.assertIn("question", c, f"第 {i} 个 case 缺 question")
                self.assertTrue(c["question"].strip())
                self.assertIn("expect_docs", c, f"第 {i} 个 case 缺 expect_docs")
                self.assertTrue(
                    isinstance(c["expect_docs"], list),
                    f"第 {i} 个 case 的 expect_docs 必须是列表",
                )

    def test_keywords_are_specific(self):
        """期望关键词不能太宽泛，否则假阳性。

        例如填 "元" 这种词，任何答案都会命中，评估就失效了。
        """
        data = self._load()
        too_generic = {"元", "亿", "的", "是", "有", "数据", "资料"}

        for c in data["cases"]:
            for kw in c.get("expect_keywords") or []:
                with self.subTest(question=c["question"][:20], keyword=kw):
                    self.assertNotIn(
                        kw, too_generic,
                        f"关键词「{kw}」太宽泛，任何答案都会命中，评估失效",
                    )

    def test_case_count_reasonable(self):
        """题量太少评估不可靠，太多跑一轮太慢。"""
        data = self._load()
        n = len(data["cases"])
        self.assertGreaterEqual(n, 15, f"只有 {n} 题，太少，评估不可靠")
        self.assertLessEqual(n, 60, f"有 {n} 题，太多，跑一轮太慢")

    def test_questions_are_unique(self):
        """重复的问题会让通过率虚高。"""
        data = self._load()
        questions = [c["question"] for c in data["cases"]]
        self.assertEqual(len(questions), len(set(questions)), "有重复的问题")


class OutputEncodingTestCase(unittest.TestCase):
    """脚本必须在中文 Windows 上能跑（不能因 emoji 崩溃）。"""

    def test_eval_all_reconfigures_stdout(self):
        """脚本开头必须有 UTF-8 重配逻辑。

        为什么测这个：Windows 控制台默认 GBK，
        print("❌") 直接抛 UnicodeEncodeError，整个脚本跑不起来。
        实测踩过。
        """
        p = pathlib.Path(__file__).parent.parent / "scripts" / "eval_all.py"
        src = p.read_text(encoding="utf-8")
        self.assertIn(
            "reconfigure(encoding=\"utf-8\")", src,
            "缺少 UTF-8 输出重配，Windows 上会因 emoji 崩溃",
        )

    def test_eval_retrieval_also_reconfigures(self):
        """老的 eval_retrieval.py 同样要有（它也有 emoji）。"""
        p = pathlib.Path(__file__).parent.parent / "scripts" / "eval_retrieval.py"
        if not p.exists():
            self.skipTest("脚本不存在")
        src = p.read_text(encoding="utf-8")
        self.assertIn(
            "reconfigure(encoding=\"utf-8\")", src,
            "eval_retrieval.py 缺少 UTF-8 重配",
        )


if __name__ == "__main__":
    unittest.main()
