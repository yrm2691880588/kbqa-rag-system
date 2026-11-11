"""健康探活测试：验证超时/异常兜底与汇总判定逻辑。

为什么这些测试重要：探活代码最怕"自己把自己搞挂"——
  依赖慢 → 探活请求卡死；依赖抛错 → 探活接口 500（监控反而瞎了）。
所以这里重点覆盖：单项超时、单项异常、汇总判定、以及配置类检查的分支。
"""

import asyncio
import unittest
from unittest.mock import patch

from app.core.config import settings
from app.services import health_check as hc


class TestRunWrapper(unittest.TestCase):
    """_run 包装器：超时与异常都必须转成"不健康"结果，而不是抛出。"""

    def test_normal_ok(self):
        async def ok_checker():
            return True, "一切正常"

        res = asyncio.run(hc._run("测试项", ok_checker))
        self.assertTrue(res["ok"])
        self.assertEqual(res["detail"], "一切正常")
        self.assertIn("cost_ms", res)

    def test_timeout_becomes_unhealthy(self):
        async def slow_checker():
            await asyncio.sleep(5)
            return True, "不该走到这里"

        # 把超时阈值改小，避免测试等待。
        # 注意这里改的是**配置项**而不是模块常量：超时现在从配置读，
        # 测试若还去 patch 那个已不存在的常量，patch 会"成功"但不影响任何行为，
        # 于是这条测试就变成了假绿（真超时没被触发，却因为 detail 里恰好有"超时"而通过）。
        old = settings.health_check_timeout
        settings.health_check_timeout = 0.05
        try:
            res = asyncio.run(hc._run("慢依赖", slow_checker))
        finally:
            settings.health_check_timeout = old
        self.assertFalse(res["ok"])
        self.assertIn("超时", res["detail"])

    def test_exception_becomes_unhealthy(self):
        async def broken_checker():
            raise ConnectionError("连不上")

        res = asyncio.run(hc._run("坏依赖", broken_checker))
        self.assertFalse(res["ok"])
        self.assertIn("ConnectionError", res["detail"])


class TestCheckers(unittest.TestCase):
    """各分项检查的判定分支（不依赖真实外部服务）。"""

    def test_llm_config_missing_key(self):
        with patch.object(settings, "llm_api_key", ""):
            ok, detail = asyncio.run(hc._check_llm_config())
        self.assertFalse(ok)
        self.assertIn("LLM_API_KEY", detail)

    def test_llm_config_ready(self):
        with patch.object(settings, "llm_api_key", "sk-test"), patch.object(settings, "llm_mock", False):
            ok, detail = asyncio.run(hc._check_llm_config())
        self.assertTrue(ok)
        self.assertIn(settings.llm_model, detail)

    def test_llm_config_mock_mode_noted(self):
        """压测模拟模式下也要显示为正常，但说明不会真实调用。"""
        with patch.object(settings, "llm_api_key", "sk-test"), patch.object(settings, "llm_mock", True):
            ok, detail = asyncio.run(hc._check_llm_config())
        self.assertTrue(ok)
        self.assertIn("模拟模式", detail)

    def test_embedding_config_missing_key(self):
        with patch.object(settings, "embedding_api_key", ""):
            ok, detail = asyncio.run(hc._check_embedding_config())
        self.assertFalse(ok)
        self.assertIn("EMBEDDING_API_KEY", detail)

    def test_upload_dir_writable(self):
        """上传目录检查：在临时目录上验证"可写"路径。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.services.ingestion.UPLOAD_ROOT", Path(tmp) / "uploads"):
                ok, detail = asyncio.run(hc._check_upload_dir())
        self.assertTrue(ok)
        self.assertIn("可写", detail)


class TestRunChecks(unittest.TestCase):
    """汇总逻辑：全部健康 / 存在不健康项。"""

    def test_all_healthy(self):
        async def ok(*_a, **_k):
            return True, "正常"

        with patch.object(hc, "_check_database", ok), patch.object(hc, "_check_qdrant", ok), \
             patch.object(hc, "_check_upload_dir", ok), patch.object(hc, "_check_llm_config", ok), \
             patch.object(hc, "_check_embedding_config", ok):
            data = asyncio.run(hc.run_checks(deep=False))
        self.assertTrue(data["healthy"])
        self.assertEqual(data["status"], "ok")
        # 基础探活应包含 5 个分项
        self.assertEqual(len(data["checks"]), 5)

    def test_one_broken_marks_unhealthy(self):
        async def ok(*_a, **_k):
            return True, "正常"

        async def bad(*_a, **_k):
            return False, "向量库不可用"

        with patch.object(hc, "_check_database", ok), patch.object(hc, "_check_qdrant", bad), \
             patch.object(hc, "_check_upload_dir", ok), patch.object(hc, "_check_llm_config", ok), \
             patch.object(hc, "_check_embedding_config", ok):
            data = asyncio.run(hc.run_checks(deep=False))
        self.assertFalse(data["healthy"])
        self.assertEqual(data["status"], "unhealthy")

    def test_deep_mode_adds_external_call_item(self):
        async def ok(*_a, **_k):
            return True, "正常"

        with patch.object(hc, "_check_database", ok), patch.object(hc, "_check_qdrant", ok), \
             patch.object(hc, "_check_upload_dir", ok), patch.object(hc, "_check_llm_config", ok), \
             patch.object(hc, "_check_embedding_call", ok):
            data = asyncio.run(hc.run_checks(deep=True))
        names = [c["name"] for c in data["checks"]]
        self.assertTrue(any("真实调用" in n for n in names), names)


if __name__ == "__main__":
    unittest.main()
