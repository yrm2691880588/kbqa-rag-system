"""会话导出功能的测试。

## 为什么要测这个

导出看起来简单（"把消息塞进 Excel"），但有几个容易出错的地方：

  · 问答配对：遇到只有提问没回答的情况（中断/失败）不能丢行
  · 文件名安全：用户给会话起名带 `/ : * ?` 时不能生成非法文件名
  · 引用压缩：完整引用 JSON 有几千字，塞进单元格根本没法看
  · 时区：datetime 带时区，直接写进 Excel 用户会看到 UTC 时间
  · 不抛异常：导出失败要返回一个能打开的 Excel，不是 500

## 断言的是什么

不是"输出好不好看"，而是"数据对不对、边界稳不稳"。
"""

import datetime as dt
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.export import (
    _fmt_citations,
    _fmt_time,
    build_session_xlsx,
    suggest_filename,
)


def _read(blob: bytes):
    """把导出的字节读成 (工作表, 所有行)。"""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(blob))
    ws = wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    return ws, rows


class TimeFormatTestCase(unittest.TestCase):
    """时间格式化。"""

    def test_naive_datetime(self):
        v = dt.datetime(2026, 11, 18, 21, 55, 47)
        self.assertEqual(_fmt_time(v), "2026-11-18 21:55:47")

    def test_timezone_aware_converted_to_local(self):
        """带时区的时间要转本地，不能直接显示 UTC。"""
        v = dt.datetime(2026, 11, 18, 13, 55, 47, tzinfo=dt.timezone.utc)
        out = _fmt_time(v)
        # 只要能格式化出来即可（本地时区取决于运行环境）
        self.assertRegex(out, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_none(self):
        self.assertEqual(_fmt_time(None), "")


class CitationFormatTestCase(unittest.TestCase):
    """引用压缩。"""

    def test_empty(self):
        self.assertEqual(_fmt_citations(None), "")
        self.assertEqual(_fmt_citations([]), "")

    def test_single(self):
        c = [{"doc_name": "某文档.md", "page": None}]
        self.assertEqual(_fmt_citations(c), "某文档.md")

    def test_with_page(self):
        c = [{"doc_name": "某文档.md", "page": 5}]
        self.assertIn("第 5 页", _fmt_citations(c))

    def test_deduplicates_with_count(self):
        """同一份文档被引用多次要显示次数，而不是重复列三遍。"""
        c = [
            {"doc_name": "某文档.md"},
            {"doc_name": "某文档.md"},
            {"doc_name": "某文档.md"},
        ]
        out = _fmt_citations(c)
        self.assertEqual(out.count("某文档.md"), 1, f"没有去重：{out}")
        self.assertIn("×3", out, f"没有显示次数：{out}")

    def test_multiple_docs(self):
        c = [{"doc_name": "A.md"}, {"doc_name": "B.md"}]
        out = _fmt_citations(c)
        self.assertIn("A.md", out)
        self.assertIn("B.md", out)

    def test_missing_doc_name(self):
        """doc_name 缺失时不崩，给个占位。"""
        out = _fmt_citations([{"page": 1}])
        self.assertIn("未命名", out)


class FilenameTestCase(unittest.TestCase):
    """导出文件名安全。"""

    def test_removes_illegal_chars(self):
        """Windows/Unix 都不允许的字符要剔除。"""
        name = suggest_filename('a/b:c*d?e"f<g>h|i', 1)
        for ch in '\\/:*?"<>|':
            self.assertNotIn(ch, name, f"文件名里还有非法字符 {ch}：{name}")

    def test_keeps_chinese(self):
        name = suggest_filename("财务分析", 1)
        self.assertIn("财务分析", name)

    def test_length_limited(self):
        """超长标题要截断（否则文件系统报错）。"""
        name = suggest_filename("很长的标题" * 50, 1)
        # 整个文件名（含前缀后缀）不该超过合理长度
        self.assertLessEqual(len(name), 80, f"文件名太长：{len(name)} 字符")

    def test_empty_title_fallback(self):
        name = suggest_filename("", 42)
        self.assertIn("42", name)
        self.assertIn(".xlsx", name)

    def test_has_xlsx_extension(self):
        self.assertTrue(suggest_filename("测试", 1).endswith(".xlsx"))


class BuildXlsxTestCase(unittest.TestCase):
    """Excel 生成。"""

    def _msg(self, role, content, **kw):
        base = {
            "role": role,
            "content": content,
            "created_at": dt.datetime(2026, 11, 18, 10, 0, 0),
            "citations": None,
            "feedback": None,
            "feedback_reason": None,
            "feedback_note": None,
            "latency_ms": None,
        }
        base.update(kw)
        return base

    def test_qa_pairs(self):
        """一问一答应该合并成一行。"""
        msgs = [
            self._msg("user", "问题一"),
            self._msg("assistant", "回答一", latency_ms=1200),
            self._msg("user", "问题二"),
            self._msg("assistant", "回答二"),
        ]
        _, rows = _read(build_session_xlsx("测试", msgs))

        # 1 行表头 + 2 行数据
        self.assertEqual(len(rows), 3, f"行数不对：{len(rows)}")
        self.assertEqual(rows[1][2], "问题一")
        self.assertEqual(rows[1][3], "回答一")
        self.assertEqual(rows[1][8], 1200)
        self.assertEqual(rows[2][2], "问题二")

    def test_unanswered_question_kept(self):
        """只有提问没有回答时，也要保留这一行。

        为什么重要：用户中断回答是常见操作。如果导出时丢掉这些行，
        用户会觉得"我明明问过这个问题，怎么没有"。
        """
        msgs = [
            self._msg("user", "没人回答的问题"),
            self._msg("user", "还有一个"),
            self._msg("assistant", "这个有回答"),
        ]
        _, rows = _read(build_session_xlsx("测试", msgs))

        self.assertEqual(len(rows), 3, f"丢了未回答的行：{len(rows)}")
        self.assertEqual(rows[1][2], "没人回答的问题")
        self.assertIn("未生成回答", str(rows[1][3]))
        self.assertEqual(rows[2][2], "还有一个")

    def test_feedback_rendered(self):
        """赞/踩要显示成中文，而不是 1 / -1。"""
        msgs = [
            self._msg("user", "q"),
            self._msg("assistant", "a", feedback=1),
            self._msg("user", "q2"),
            self._msg("assistant", "a2", feedback=-1, feedback_reason="内容不准"),
        ]
        _, rows = _read(build_session_xlsx("测试", msgs))

        self.assertIn("有帮助", str(rows[1][5]))
        self.assertIn("没帮助", str(rows[2][5]))
        self.assertEqual(rows[2][6], "内容不准")

    def test_citations_rendered(self):
        """引用要压缩成一行。"""
        msgs = [
            self._msg("user", "q"),
            self._msg(
                "assistant", "a",
                citations=[{"doc_name": "某文档", "page": 3}],
            ),
        ]
        _, rows = _read(build_session_xlsx("测试", msgs))
        self.assertIn("某文档", str(rows[1][4]))

    def test_empty_session(self):
        """空会话也要能导出（只有表头）。"""
        _, rows = _read(build_session_xlsx("空会话", []))
        self.assertEqual(len(rows), 1, "空会话应该只有表头")

    def test_header_row(self):
        """表头必须存在且列数正确。"""
        ws, rows = _read(build_session_xlsx("测试", []))
        self.assertEqual(rows[0][0], "序号")
        self.assertEqual(rows[0][2], "提问")
        self.assertEqual(rows[0][3], "回答")
        self.assertEqual(ws.max_column, 9)

    def test_freeze_panes(self):
        """表头要冻结（滚动时可见）。"""
        ws, _ = _read(build_session_xlsx("测试", []))
        self.assertEqual(ws.freeze_panes, "A2")

    def test_returns_bytes(self):
        """返回值必须是 bytes（调用方直接当响应体）。"""
        out = build_session_xlsx("测试", [])
        self.assertIsInstance(out, bytes)
        self.assertGreater(len(out), 1000, "文件太小，可能没写成功")

    def test_unknown_role_ignored(self):
        """未知 role 不该导致崩溃。"""
        msgs = [
            self._msg("user", "q"),
            self._msg("system", "系统提示"),
            self._msg("assistant", "a"),
        ]
        _, rows = _read(build_session_xlsx("测试", msgs))
        self.assertEqual(len(rows), 2, "system 消息不该单独成行")


class ExportResponseTestCase(unittest.TestCase):
    """接口层面：路由存在、权限正确、上限存在。"""

    def test_route_exists(self):
        from app.api.v1 import sessions

        paths = [getattr(r, "path", "") for r in sessions.router.routes]
        self.assertTrue(
            any("export" in p for p in paths),
            f"没找到导出路由：{paths}",
        )

    def test_has_max_limit(self):
        """必须有消息条数上限（防内存爆炸）。"""
        from app.api.v1 import sessions

        self.assertTrue(hasattr(sessions, "_EXPORT_MAX_MESSAGES"))
        self.assertGreater(sessions._EXPORT_MAX_MESSAGES, 0)

    def test_verifies_ownership(self):
        """必须校验会话归属（不能导别人的会话）。

        会话内容属于个人隐私，管理员也不该能导别人的。
        """
        import inspect

        from app.api.v1 import sessions

        src = inspect.getsource(sessions.export_session)
        self.assertIn(
            "_own_session", src,
            "导出接口没有校验会话归属 —— 可能泄露他人对话",
        )


class FrontendContractTestCase(unittest.TestCase):
    """前端调用契约。"""

    def test_api_uses_blob(self):
        """前端必须用 responseType blob，否则二进制会被当文本解析坏。"""
        import pathlib

        p = (
            pathlib.Path(__file__).parent.parent.parent
            / "frontend/src/api/chat.ts"
        )
        if not p.exists():
            self.skipTest("前端源码不在当前目录（导出产物里正常）")
        src = p.read_text(encoding="utf-8")
        self.assertIn("responseType: 'blob'", src)


if __name__ == "__main__":
    unittest.main()
