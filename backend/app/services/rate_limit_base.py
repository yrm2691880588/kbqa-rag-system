"""限流器的共用基类。

为什么要有这个文件（大白话）：
    项目里有两套限流，算法不同、场景不同：
      · 问答限流（ai_guard.RateLimiter）—— 令牌桶
        允许小幅突发、平滑补充，适合"用户可能连点几下"的聊天场景。
      · 付费端点限流（health._SlidingWindowLimiter）—— 滑动窗口
        严格保证"最近 60 秒内不超过 N 次"，适合"每调一次都花钱"的端点。

    两者的**算法必须不同**（强行合成一套会破坏其中一个场景的语义），
    但它们有一批**完全相同**的东西：
      · 都按 key（组织 id / 用户 id）分开计数
      · 都要清理长期不用的 key，否则字典会越涨越大
      · 都要一个 reset() 供测试使用

    这个基类只提供这三样，算法留给子类。
"""

from __future__ import annotations

import time


class BaseKeyedLimiter:
    """按 key 计数的限流器基类（只管状态容器与清理，不含具体算法）。"""

    # 超过这么久没活动的 key，清理时会被丢掉。
    # 定得比最长的限流窗口大一个量级即可 —— 目的只是防止字典无限增长。
    IDLE_TTL_SECONDS: float = 3600.0

    def __init__(self) -> None:
        # 子类自行决定 value 的结构；基类只负责按 key 存取与清理
        self._state: dict[str, tuple[float, object]] = {}   # key -> (最后活跃时间, 状态)

    def _now(self) -> float:
        """当前单调时钟。抽成方法便于测试注入固定时间。"""
        return time.monotonic()

    def _get_state(self, key: str):
        """取某个 key 的状态；不存在返回 None。"""
        item = self._state.get(key)
        return item[1] if item else None

    def _set_state(self, key: str, state, now: float | None = None) -> None:
        """存某个 key 的状态，并记下活跃时间（供清理用）。"""
        self._state[key] = (now if now is not None else self._now(), state)

    def purge_idle(self, ttl: float | None = None) -> int:
        """清掉长期不活跃的 key，返回清理条数。

        为什么需要：限流状态是按 key 存的字典。如果不清理，
        每来过一个新组织/新用户就永久多一个条目 ——
        长期运行后内存只涨不降（这类泄漏通常几个月才被发现）。
        """
        limit = self.IDLE_TTL_SECONDS if ttl is None else ttl
        cutoff = self._now() - limit
        stale = [k for k, (ts, _) in self._state.items() if ts < cutoff]
        for k in stale:
            self._state.pop(k, None)
        return len(stale)

    def reset(self) -> None:
        """清空全部状态（测试用）。"""
        self._state.clear()

    def key_count(self) -> int:
        """当前追踪了多少个 key（测试/监控用）。"""
        return len(self._state)
