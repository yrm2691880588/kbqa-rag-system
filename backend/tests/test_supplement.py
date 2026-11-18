"""关键词补充逻辑的测试。

## 这段逻辑的迭代过程（3 次失败才做对，值得记录）

### 第 1 版：全量 RRF 融合 —— 失败

    纯向量：4/6 → 混合 3/6（净 -1）
    「负责人」本应命中组织架构文档，融合后只剩投委会纪要。

原因：关键词提取不准时拉进大量噪音，RRF 让噪音"多路命中"压过向量判断。

### 第 2 版：追加到末尾 —— 无效

    `if len(out) >= final_k: break`
    向量结果 5 条已占满名额 → 一条都补不进来。

### 第 3 版：替换末尾 —— 有改善但伤 Hit@1

    内容覆盖 +2，但 Hit@1 从 7/15 掉到 5/15。
    原因：结果少时（1-2 条）替换会踢掉向量精选的结果。

### 第 4 版（当前）：判定收紧 + 少结果不替换

    ✅ 内容改善 3 个（不良率、净息差、渠道）
    ✅ Hit@1 / Hit@3 完全无损
    ✅ 无一个 case 变差

## 这组测试保护什么

  · "缺词判定"用 all 而不是 any
  · 缺失词的核心子串已覆盖时不补充（防「预算执行率」误判）
  · 结果少于 3 条时只追加不替换（防 Hit@1 下降）
  · 补充失败不能影响向量结果
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class SupplementGuardTestCase(unittest.TestCase):
    """补充逻辑的判定条件（纯逻辑，不需要真实检索）。"""

    def test_short_results_no_replace(self):
        """向量结果少于 3 条时只追加不替换。

        实测依据：替换会让 Hit@1 从 7/15 掉到 5/15。
        结果少说明向量已经筛得很严，不该动它的排序。
        """
        from app.services.retriever import _supplement_with_keywords  # noqa: F401

        # 这个行为通过代码里的 can_replace 判断实现，
        # 这里做静态检查，确保那道保护还在
        import inspect

        src = inspect.getsource(_supplement_with_keywords)
        self.assertIn(
            "can_replace = len(out) >= 3", src,
            "缺少「结果少时不替换」的保护 —— 会导致 Hit@1 下降",
        )

    def test_missing_check_uses_core_coverage(self):
        """缺词判定要看核心子串是否覆盖，不能只看字面。"""
        import inspect

        from app.services.retriever import _supplement_with_keywords

        src = inspect.getsource(_supplement_with_keywords)
        self.assertIn(
            "_core_covered", src,
            "缺少核心子串覆盖判定 —— 「预算执行率」这类复合词会被误判为缺失",
        )

    def test_max_replace_limit_exists(self):
        """替换条数必须有上限，否则会退化成全量融合（已知失败方案）。"""
        from app.services.retriever import _MAX_REPLACE

        self.assertIsInstance(_MAX_REPLACE, int)
        self.assertGreater(_MAX_REPLACE, 0)
        self.assertLessEqual(
            _MAX_REPLACE, 3,
            f"替换上限 {_MAX_REPLACE} 太大，会打乱向量排序（全量 RRF 失败的原因）",
        )

    def test_supplement_after_truncate(self):
        """关键词补充必须在「截断」之后，不能在之前。

        实测踩过：先补充再截断，补进来的片段在末尾，被 hits[:final_k] 切掉，
        整个补充逻辑白做（净息差一直修不好就是因为这个）。
        """
        import inspect

        from app.services.retriever import retrieve

        src = inspect.getsource(retrieve)
        pos_truncate = src.find("hits = hits[:final_k]")
        pos_supplement = src.find("_supplement_with_keywords(")

        self.assertGreater(pos_truncate, 0, "找不到截断语句")
        self.assertGreater(pos_supplement, 0, "找不到补充调用")
        self.assertLess(
            pos_truncate, pos_supplement,
            "补充在截断之前 —— 补进来的片段会被截掉，逻辑失效",
        )

    def test_supplement_failure_does_not_break_retrieval(self):
        """补充失败不能影响向量结果（必须有 try/except）。"""
        import inspect

        from app.services.retriever import retrieve

        src = inspect.getsource(retrieve)
        idx = src.find("_supplement_with_keywords(")
        self.assertGreater(idx, 0)
        # 调用点往前找 try
        before = src[max(0, idx - 500) : idx]
        self.assertIn("try:", before, "补充调用没有 try 包裹")


class KeywordExtractTestCase(unittest.TestCase):
    """关键词提取（不需要真实检索）。"""

    def setUp(self):
        from app.services.vector_store import extract_keywords

        self.extract = extract_keywords

    def test_extracts_content_words(self):
        """能提取出问句里的实词。"""
        kws = self.extract("海联银行的净息差是多少？")
        self.assertIn("净息差", kws, f"没提取出「净息差」：{kws}")

    def test_strips_question_words(self):
        """疑问词不应出现在关键词里。"""
        kws = self.extract("净利润是多少？")
        for bad in ("是多少", "多少", "什么"):
            self.assertNotIn(bad, kws, f"疑问词「{bad}」没被过滤：{kws}")

    def test_long_phrase_split_into_ngrams(self):
        """长问句要切出 n-gram，否则文档里匹配不到。"""
        kws = self.extract("投委会对渠道扩张是怎么决定的？")
        # 「投委会对渠道扩张」整串在文档里不存在，必须切出「渠道扩张」
        self.assertIn("渠道扩张", kws, f"没切出「渠道扩张」：{kws}")

    def test_empty_question(self):
        self.assertEqual(self.extract(""), [])
        self.assertEqual(self.extract("   "), [])

    def test_result_count_limited(self):
        """关键词数量要有上限，避免每次检索发几十个请求。"""
        kws = self.extract("请详细说明海联银行2025年度营业收入净利润总资产不良贷款率拨备覆盖率的对比情况")
        self.assertLessEqual(len(kws), 10, f"提取了 {len(kws)} 个关键词，太多")


class RerankLocalConfigTestCase(unittest.TestCase):
    """本地重排的配置与降级行为。"""

    def test_local_url_enables_rerank(self):
        """配了本地地址就算启用重排。"""
        from app.core.config import settings
        from app.services import rerank

        old_local = settings.rerank_local_url
        old_key = settings.rerank_api_key
        old_model = settings.rerank_model
        try:
            settings.rerank_local_url = "http://127.0.0.1:8020"
            settings.rerank_api_key = ""
            settings.rerank_model = ""
            self.assertTrue(rerank.is_enabled(), "配了本地地址应算启用")
        finally:
            settings.rerank_local_url = old_local
            settings.rerank_api_key = old_key
            settings.rerank_model = old_model

    def test_nothing_configured_disables(self):
        """什么都没配就不启用。"""
        from app.core.config import settings
        from app.services import rerank

        old_local = settings.rerank_local_url
        old_key = settings.rerank_api_key
        old_model = settings.rerank_model
        try:
            settings.rerank_local_url = ""
            settings.rerank_api_key = ""
            settings.rerank_model = ""
            self.assertFalse(rerank.is_enabled())
        finally:
            settings.rerank_local_url = old_local
            settings.rerank_api_key = old_key
            settings.rerank_model = old_model

    def test_local_failure_returns_none(self):
        """本地服务不可达时返回 None（调用方据此降级）。"""
        import asyncio

        from app.core.config import settings
        from app.services import rerank

        old_local = settings.rerank_local_url
        old_timeout = settings.rerank_timeout_seconds
        try:
            # 指向一个必然连不上的端口
            settings.rerank_local_url = "http://127.0.0.1:59999"
            settings.rerank_timeout_seconds = 1.0
            # 用 asyncio.run 而不是 get_event_loop：
            # 后者在 Python 3.12 已弃用，且在"已有事件循环"的上下文里会报
            # "There is no current event loop"。
            result = asyncio.run(rerank._rerank_local("测试", ["文档"]))
            self.assertIsNone(result, "连不上应返回 None，让调用方降级")
        finally:
            settings.rerank_local_url = old_local
            settings.rerank_timeout_seconds = old_timeout


if __name__ == "__main__":
    unittest.main()
