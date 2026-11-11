"""AI 调用治理：限流、熔断、问题缓存。

为什么需要这三样（都是为了"高峰别崩、成本别乱"）：

  ① **短期限流（削峰）**
     现有的额度是按天/按月算的（防"一个月被刷爆"），但挡不住"某一分钟涌进来 200 个请求"。
     大模型接口本身也有速率上限，超了就会返回 429，用户看到的是"报错"。
     所以再加一层"每分钟最多几次"的闸门，把尖峰削平。

  ② **熔断（别陪着慢依赖一起死）**
     如果大模型接口开始大面积超时，每个请求都要干等 120 秒才失败，
     很快所有工作线程都被占满 —— 结果是"大模型挂了，把我们的服务也拖垮了"。
     熔断器的逻辑和家里的保险丝一样：连续失败几次就"跳闸"，
     接下来一段时间直接快速失败（立刻回一句"服务繁忙请稍后"），
     等过一会儿再放一两个请求去试探，好了就恢复。这样至少网站还能打开。

  ③ **问题缓存（别重复花钱）**
     "公司的地址在哪？"这种问题一天可能被不同人问几十遍，
     每次都要重新向量检索 + 调一次大模型。把答案缓存起来，
     同样的问法直接返回，又快又省钱。

    ⚠️ 缓存有一条**必须守住的铁律**：缓存键里一定要带"是谁在问、能看哪些库"。
      否则企业 A 问过的问题，企业 B 问同样的话就会拿到 A 的答案 ——
      这是跨客户数据泄露，比"多花点钱"严重一万倍。
      所以本文件把"范围"作为缓存键的**必填项**，不传就不启用缓存。

三个组件都支持注入"时间来源"，这样测试可以随意把时间往前拨，不用真的等。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

logger = logging.getLogger("kbqa.ai_guard")


def _now() -> float:
    return time.monotonic()


# ============================================================
# ① 短期限流：令牌桶
# ============================================================


class RateLimiter:
    """令牌桶限流器（按"谁"分别计数）。

    大白话：给每个客户组织发一个桶，桶里装着"这一分钟能问几次"的令牌。
    每问一次就取走一个令牌；取完了就得等桶里重新攒出来。
    桶是**匀速补满**的，所以请求再多也不会出现"整分钟不能用、然后突然放一批"的抖动。

    ⚠️ 一个诚实的说明（别被它骗了）：
        这是**进程内**的桶。生产环境如果跑 4 个进程，实际能通过的请求最多是配置值的 4 倍。
        为什么可以接受：真正防"成本失控"的是按天/按月的那道额度（那个存在数据库里，
        多进程统计仍准确）；这一层只负责"削平尖峰、别把外部接口打限流"，
        多算少算百分之几十没关系。
        如果哪天需要精确到个位，把它换成 Redis 计数器即可（这里已经隔离成一个类，换起来不影响别处）。
    """

    def __init__(self, per_minute: int = 0):
        # 0 或负数 = 不限流（默认就是这个状态，不影响现有行为）
        self.per_minute = max(0, int(per_minute or 0))
        self._buckets: dict[str, tuple[float, float]] = {}   # key -> (令牌数, 上次补充时间)

    def configure(self, per_minute: int) -> None:
        self.per_minute = max(0, int(per_minute or 0))

    def allow(self, key: str, cost: float = 1.0, now: float | None = None) -> bool:
        """尝试取走一个令牌；取到了返回 True，取不到返回 False。"""
        if self.per_minute <= 0:
            return True

        now = now if now is not None else _now()
        capacity = float(self.per_minute)
        # 补充速度：每秒钟补多少令牌（一分钟补满一整桶）
        refill_per_second = capacity / 60.0

        tokens, last = self._buckets.get(key, (capacity, now))
        tokens = min(capacity, tokens + max(0.0, now - last) * refill_per_second)

        if tokens >= cost:
            self._buckets[key] = (tokens - cost, now)
            return True
        self._buckets[key] = (tokens, now)
        return False

    def retry_after_seconds(self, key: str, now: float | None = None) -> int:
        """还要等几秒才能再问（给用户一个明确的"请 N 秒后再试"）。"""
        if self.per_minute <= 0:
            return 0
        now = now if now is not None else _now()
        tokens, last = self._buckets.get(key, (float(self.per_minute), now))
        tokens = min(float(self.per_minute), tokens + max(0.0, now - last) * (self.per_minute / 60.0))
        if tokens >= 1.0:
            return 0
        need = 1.0 - tokens
        return max(1, int(need / (self.per_minute / 60.0)) + 1)

    def reset(self) -> None:
        self._buckets.clear()


rate_limiter = RateLimiter()


# ============================================================
# ② 熔断器
# ============================================================

CLOSED = "closed"        # 正常通行
OPEN = "open"            # 跳闸了，直接快速失败
HALF_OPEN = "half_open"  # 试探中：放一两个请求出去看看


@dataclass
class BreakerState:
    state: str = CLOSED
    failures: int = 0
    opened_at: float = 0.0
    trials: int = 0


class CircuitBreaker:
    """熔断器：连续失败到阈值就"跳闸"，过一段时间再试探恢复。

    状态机（就三个状态，别搞复杂）：
        closed   —— 正常。失败次数累加，到阈值 → open
        open     —— 跳闸。所有请求立刻失败（不真的去调外部），
                    过了冷却时间 → half_open
        half_open—— 试探。只放少量请求出去：成功 → closed（恢复正常）；
                    又失败 → 立刻回到 open（说明外部还没好，继续快速失败）

    为什么要"快速失败"而不是"继续老实等待"：
        外部接口坏了的时候，每个请求都干等两分钟才失败，
        工作线程会迅速被占满 —— 于是"外部服务坏了"变成"我们的网站也打不开了"。
        快速失败能保住网站本身还能正常打开（至少能告诉用户"稍后再试"）。
    """

    def __init__(self, fail_threshold: int = 5, open_seconds: float = 60.0,
                 half_open_trials: int = 1, enabled: bool = True):
        self.fail_threshold = max(1, int(fail_threshold or 1))
        self.open_seconds = max(1.0, float(open_seconds or 60.0))
        self.half_open_trials = max(1, int(half_open_trials or 1))
        self.enabled = enabled
        self._s = BreakerState()

    @property
    def state(self) -> str:
        return self._s.state

    @property
    def failure_count(self) -> int:
        return self._s.failures

    def configure(self, fail_threshold: int, open_seconds: float,
                  half_open_trials: int = 1, enabled: bool = True) -> None:
        self.fail_threshold = max(1, int(fail_threshold or 1))
        self.open_seconds = max(1.0, float(open_seconds or 60.0))
        self.half_open_trials = max(1, int(half_open_trials or 1))
        self.enabled = enabled

    def allow(self, now: float | None = None) -> bool:
        """现在允不允许真的去调外部服务？"""
        if not self.enabled:
            return True
        now = now if now is not None else _now()
        s = self._s

        if s.state == OPEN:
            if now - s.opened_at >= self.open_seconds:
                # 冷却结束：进入试探状态
                s.state = HALF_OPEN
                s.trials = 0
                logger.info("熔断器进入试探状态（冷却 %.0f 秒已过）", self.open_seconds)
            else:
                return False

        if s.state == HALF_OPEN and s.trials >= self.half_open_trials:
            # 试探名额已经用完，等这一批的结果
            return False
        return True

    def record_success(self) -> None:
        """一次成功的调用：直接恢复（试探成功也走这里）。"""
        if self._s.state != CLOSED:
            logger.info("熔断器恢复正常（closed）")
        self._s = BreakerState(state=CLOSED)

    def record_failure(self, now: float | None = None) -> None:
        """一次失败的调用。"""
        if not self.enabled:
            return
        now = now if now is not None else _now()
        s = self._s

        if s.state == HALF_OPEN:
            # 试探失败：立刻重新跳闸，说明外部还没好
            s.state = OPEN
            s.opened_at = now
            s.trials = 0
            logger.warning("试探失败，熔断器重新跳闸（%.0f 秒内快速失败）", self.open_seconds)
            return

        s.failures += 1
        if s.state == CLOSED and s.failures >= self.fail_threshold:
            s.state = OPEN
            s.opened_at = now
            logger.warning(
                "连续失败 %d 次，熔断器跳闸：接下来 %.0f 秒内直接快速失败，"
                "避免把外部服务的故障拖成我们自己的故障",
                s.failures, self.open_seconds,
            )

    def record_trial(self) -> None:
        """记录"已放出一个试探请求"（避免试探期间涌进一堆请求）。"""
        if self._s.state == HALF_OPEN:
            self._s.trials += 1

    def reset(self) -> None:
        self._s = BreakerState()


llm_breaker = CircuitBreaker()


# ============================================================
# ③ 问题缓存
# ============================================================

_WS = re.compile(r"\s+")


def normalize_question(text: str) -> str:
    """把问题规整成"比较用"的形式。

    做两件事：把连续空白压成一个空格、去掉首尾空白、统一小写。
    为什么不做更多（比如去掉标点）：
      去得太狠会让"要不要退货？"和"要不要退货"共用一份缓存 —— 看起来是好事，
      但万一将来答案依赖标点（语气/否定），就会给出错误答案。
      缓存宁可少命中几次，也不要给出"看着像但其实不对"的答案。
    """
    return _WS.sub(" ", (text or "").strip()).lower()


def normalize_scope(scope) -> str:
    """把"可见范围"规整成稳定的字符串。

    None 和 [] 必须区分开（一个是"不限制"、一个是"什么都看不到"），
    所以这里显式转成 'ALL' / 'NONE'，而不是靠 str(None) 之类的默认行为 ——
    这个区分在本项目里是有血的教训的。
    """
    if scope is None:
        return "ALL"
    items = sorted({int(x) for x in scope})
    if not items:
        return "NONE"
    return ",".join(str(i) for i in items)


def make_cache_key(question: str, kb_ids, scope_key: str) -> str:
    """生成缓存键。

    ⚠️ **必须带上范围（scope_key）和知识库集合**。这是安全红线：
       缓存键如果只看"问题原文"，那企业 A 问过"公司地址在哪"，
       企业 B 问同样一句就会拿到 A 公司知识库里的答案 —— 跨客户泄露。
       把范围写进键里，不同客户即使问得一模一样，也是两条互不相干的缓存。
    """
    raw = "|".join([
        normalize_scope(kb_ids),
        (scope_key or "").strip(),
        normalize_question(question),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class CacheEntry:
    payload: dict
    expires_at: float
    hits: int = 0


class QuestionCache:
    """按问题缓存回答（进程内、带过期时间、超量淘汰最久未用的）。

    三个参数的含义：
      · ttl_seconds  —— 一条缓存活多久。太短没意义，太长会"知识库更新了还在答老话"。
      · max_entries  —— 最多存多少条。超出时淘汰"最久没被用到"的那条（LRU）。
                        为什么要有上限：不限量的话，被刷一堆奇葩问题就把内存吃光了。
      · enabled      —— 总开关。
    """

    def __init__(self, ttl_seconds: int = 3600, max_entries: int = 500, enabled: bool = False):
        self.ttl_seconds = max(0, int(ttl_seconds or 0))
        self.max_entries = max(0, int(max_entries or 0))
        self.enabled = enabled
        self._data: OrderedDict[str, CacheEntry] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def configure(self, ttl_seconds: int, max_entries: int, enabled: bool) -> None:
        self.ttl_seconds = max(0, int(ttl_seconds or 0))
        self.max_entries = max(0, int(max_entries or 0))
        self.enabled = enabled

    def get(self, key: str, now: float | None = None) -> dict | None:
        if not self.enabled:
            return None
        now = now if now is not None else _now()
        entry = self._data.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_at <= now:
            # 过期了：顺手删掉（不然内存里会攒一堆再也用不到的条目）
            self._data.pop(key, None)
            self.misses += 1
            return None
        entry.hits += 1
        self._data.move_to_end(key)     # 标记为"最近用过"（LRU 淘汰时保住它）
        self.hits += 1
        return entry.payload

    def put(self, key: str, payload: dict, now: float | None = None) -> None:
        if not self.enabled:
            return
        now = now if now is not None else _now()
        self._data[key] = CacheEntry(payload=payload, expires_at=now + self.ttl_seconds)
        self._data.move_to_end(key)
        while self.max_entries and len(self._data) > self.max_entries:
            self._data.popitem(last=False)      # 淘汰最久未使用的那条

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "entries": len(self._data),
            "max_entries": self.max_entries,
            "ttl_seconds": self.ttl_seconds,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }

    def clear(self) -> None:
        self._data.clear()
        self.hits = 0
        self.misses = 0


question_cache = QuestionCache()


def reload_from_settings() -> None:
    """把配置里的参数应用到三个组件上（启动时或配置热更新后调用）。"""
    from app.core.config import settings

    rate_limiter.configure(int(getattr(settings, "ai_rate_limit_per_minute", 0) or 0))
    llm_breaker.configure(
        fail_threshold=int(getattr(settings, "ai_cb_fail_threshold", 5) or 5),
        open_seconds=float(getattr(settings, "ai_cb_open_seconds", 60) or 60),
        half_open_trials=int(getattr(settings, "ai_cb_half_open_trials", 1) or 1),
        enabled=bool(getattr(settings, "ai_cb_enabled", True)),
    )
    question_cache.configure(
        ttl_seconds=int(getattr(settings, "qa_cache_ttl_seconds", 3600) or 3600),
        max_entries=int(getattr(settings, "qa_cache_max_entries", 500) or 500),
        enabled=bool(getattr(settings, "qa_cache_enabled", False)),
    )
