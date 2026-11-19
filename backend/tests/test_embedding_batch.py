"""向量化批次大小的测试。

## 这个 bug 的由来

`embedding_batch_size` 原来配的是 16，但 DashScope text-embedding-v4
单次请求最多接受 **10** 条文本，超过直接返回 400：
    "batch size is invalid, it should not be larger than 10"

为什么之前没发现：
  · 普通 .md 文档切出来的片段少（6~9 个），凑不满 16，所以从没触发
  · Excel 有 40 个片段，第一批塞 16 条 → 直接被拒 → 表现为"入库失败"

这类 bug 的隐蔽性在于：**文档越短越不会触发**。
所以测试必须覆盖"片段数超过上限"的情况。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.services.vectorizer import _MAX_EMBED_BATCH


class EmbedBatchLimitTestCase(unittest.TestCase):
    """批次大小必须遵守供应商上限。"""

    def test_max_batch_constant_is_10(self):
        """供应商硬限制是 10，这个常量不能随便改大。"""
        self.assertEqual(
            _MAX_EMBED_BATCH, 10,
            "DashScope text-embedding-v4 单次最多 10 条文本，改大必然失败",
        )

    def test_default_config_within_limit(self):
        """配置的默认值不能超过上限。"""
        self.assertLessEqual(
            settings.embedding_batch_size, _MAX_EMBED_BATCH,
            f"embedding_batch_size={settings.embedding_batch_size} "
            f"超过供应商上限 {_MAX_EMBED_BATCH}",
        )

    def test_batching_logic_covers_long_document(self):
        """模拟 40 个片段的分批：每批都不能超上限。

        这是真实踩到的场景：Excel 40 个片段。
        """
        total = 40
        batch_size = max(1, min(settings.embedding_batch_size, _MAX_EMBED_BATCH))

        starts = list(range(0, total, batch_size))
        for start in starts:
            actual = min(batch_size, total - start)
            self.assertLessEqual(
                actual, _MAX_EMBED_BATCH,
                f"第 {start // batch_size + 1} 批有 {actual} 条，超过上限",
            )

        # 批次要能覆盖全部片段
        covered = sum(min(batch_size, total - s) for s in starts)
        self.assertEqual(covered, total, "分批后没有覆盖全部片段")

    def test_guard_clamps_misconfigured_value(self):
        """即使配置被改成 100，运行时也要夹到 10。

        为什么需要这层保护：
            配置可以被 .env 覆盖，写错了就会让整个文件入库失败。
            夹一下的代价只是多几次请求，比"文件传不进去"轻得多。
        """
        for configured in (1, 5, 10, 16, 100):
            effective = max(1, min(configured, _MAX_EMBED_BATCH))
            self.assertLessEqual(effective, _MAX_EMBED_BATCH)
            self.assertGreaterEqual(effective, 1)


if __name__ == "__main__":
    unittest.main()
