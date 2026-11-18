"""流式回答保存的测试。

## 这个 bug 的排查过程（值得记录）

用户反馈：切到别的会话再切回来，回答没了，只剩提问。

排查了两层才发现完整原因：

### 前端两层

1. `switchSession` 调 `abortCtrl.abort()` —— 切换即中断请求
2. 事件回调 `if (!item) return` —— 切换后 displayMessages 被清空，
   后续事件全部丢弃

### 后端两层

3. `except GeneratorExit` 分支不保存任何内容
4. 想在那里补保存，遇到两个硬限制：
   · `GeneratorExit` 里 **不能 await**（RuntimeError: async generator ignored GeneratorExit）
   · 改用 `create_task` 也不行 —— Starlette 会取消整个请求任务，
     子任务一起被取消（实测日志：Cancelled via cancel scope）

### 最终方案

**边生成边落库**（`_StreamSaver`），而不是等断开时才存。
这样无论客户端何时断开，库里都已经有最新一版。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class StreamSaverLogicTestCase(unittest.TestCase):
    """_StreamSaver 的纯逻辑部分（不需要数据库）。"""

    def setUp(self):
        from app.api.v1.chat import _StreamSaver

        self.Saver = _StreamSaver

    def test_threshold_controls_flush(self):
        """没到阈值不写库，到了才写 —— 避免每个字都写一次。"""
        s = self.Saver(session_id=1, citations=[], threshold=120)

        self.assertFalse(s.should_flush("a" * 50), "50 字不应触发")
        self.assertFalse(s.should_flush("a" * 119), "119 字不应触发")
        self.assertTrue(s.should_flush("a" * 120), "120 字应触发")
        self.assertTrue(s.should_flush("a" * 500), "超过阈值应触发")

    def test_threshold_is_incremental(self):
        """阈值是**增量**判断，不是总量判断。

        为什么重要：如果按总量判断，写完一次之后每次都会满足条件，
        变成每块内容都写库，就失去节流意义了。
        """
        s = self.Saver(session_id=1, citations=[], threshold=100)

        self.assertTrue(s.should_flush("a" * 100))
        # 模拟已写完 100 字
        s.last_len = 100

        self.assertFalse(s.should_flush("a" * 150), "只多了 50 字，不该再写")
        self.assertTrue(s.should_flush("a" * 200), "多了 100 字，应该写")

    def test_message_id_starts_none(self):
        """初始没有 message_id（还没写过库）。"""
        s = self.Saver(session_id=1, citations=[])
        self.assertIsNone(s.message_id)

    def test_citations_normalized(self):
        """citations 传 None 时应归一成空列表，避免下游 None 判断。"""
        s = self.Saver(session_id=1, citations=None)
        self.assertEqual(s.citations, [])


class SavePathTestCase(unittest.TestCase):
    """检查保存路径的实现约束（静态检查，不起数据库）。"""

    def test_generator_exit_does_not_await(self):
        """GeneratorExit 分支里不能出现 await。

        为什么：生成器正在销毁，await 会抛
        "RuntimeError: async generator ignored GeneratorExit"。
        实测踩过。
        """
        import inspect

        from app.api.v1 import chat

        src = inspect.getsource(chat.chat)
        # 找到 GeneratorExit 分支
        idx = src.find("except GeneratorExit:")
        self.assertGreater(idx, 0, "找不到 GeneratorExit 分支")

        # 截到下一个 except
        nxt = src.find("except ", idx + 10)
        branch = src[idx:nxt] if nxt > 0 else src[idx:]

        # 允许出现 "await" 字样在注释里，但要排除真正的 await 语句
        code_lines = [
            ln for ln in branch.split(chr(10))
            if ln.strip().startswith("await ") or " await " in ln
        ]
        # 允许 create_task 里的（那不算 await）
        real_awaits = [ln for ln in code_lines if "create_task" not in ln]
        self.assertEqual(
            real_awaits, [],
            f"GeneratorExit 分支里出现了 await，会抛 RuntimeError：{real_awaits}",
        )

    def test_incremental_saver_exists(self):
        """必须有增量保存器，而不是只在结束时保存一次。"""
        from app.api.v1 import chat

        self.assertTrue(
            hasattr(chat, "_StreamSaver"),
            "缺少增量保存器 —— 客户端断开时会丢答案",
        )

    def test_flush_called_during_streaming(self):
        """流式循环里要调用 flush，不能只在结束时保存。"""
        import inspect

        from app.api.v1 import chat

        src = inspect.getsource(chat.chat)
        self.assertIn(
            "stream_saver.flush", src,
            "流式过程中没有增量落库调用",
        )
        self.assertIn(
            "should_flush", src,
            "没有按阈值节流，可能会每个字都写库",
        )

    def test_final_save_reuses_saver(self):
        """正常结束时应该复用 saver.finish()，而不是重新 INSERT。

        否则同一个回答会在库里出现两次（增量写的 + 结束写的）。
        """
        import inspect

        from app.api.v1 import chat

        src = inspect.getsource(chat.chat)
        self.assertIn(
            "stream_saver.finish", src,
            "正常结束没有复用增量保存器，会产生重复消息",
        )


if __name__ == "__main__":
    unittest.main()
