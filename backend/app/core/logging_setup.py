"""日志与请求追踪（P2-19）：让"出了问题怎么查"这件事变简单。

现状有什么问题（大白话）：
    现在日志是一行行散开的文字。出了故障，运维只能翻文件、靠时间和关键字硬猜。
    而且**没法把一次请求的多条日志串起来** —— 一个用户报"我点了没反应"，
    你只能在那几分钟的日志里大海捞针。

这里做两件事：

  ① **给每个请求发一个编号（请求 ID）**。
     同一个请求产生的所有日志都会带上这个编号。
     于是"查一次请求发生了什么"就变成一条命令：
         grep <请求ID> app.log
     编号从哪来：优先用调用方带过来的（`X-Request-ID` 请求头）——
     这样前端、网关、后端三边的日志能用同一个编号串起来；
     调用方没带就我们自己生成一个，并在响应头里回给它，
     方便用户/前端把编号报给客服。

  ② **日志可以输出成 JSON**（可选）。
     好处是能被日志系统（ELK、Loki、云日志）直接结构化检索，
     不用写正则去猜。默认还是人看的文字格式 —— 本机开发时 JSON 太难读了，
     所以由配置决定，默认关。

还有一个容易被忽略但很关键的点：
    **不要把敏感信息写进日志**。这里专门做了一个"脱敏"步骤，
    把长相像密钥、像手机号的内容替换掉。
    因为日志文件往往权限很松（运维群里随手发一段出来排查很常见），
    一旦密钥进了日志，等于泄露。
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import re
import uuid

# 当前请求的编号。用 contextvars 而不是全局变量：
# 因为一个进程会同时处理多个请求，全局变量会互相覆盖，
# 结果就是"张三的日志里出现了李四的编号"，比没有编号更误导。
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

# 请求编号的响应头名字（前端/网关都按这个约定来）
REQUEST_ID_HEADER = "X-Request-ID"


def new_request_id() -> str:
    """生成一个短一点的请求编号。

    为什么不用完整的 uuid4：日志里每一行都要带它，
    太长会让日志变得很难读（36 位）。12 位十六进制已经足够在
    实际并发量下不重复（一天百万级请求也基本不会撞）。
    """
    return uuid.uuid4().hex[:12]


def get_request_id() -> str:
    return request_id_var.get()


def set_request_id(value: str = "") -> str:
    """设置当前请求编号（空则由调用方决定是否新建）。"""
    rid = (value or "").strip() or new_request_id()
    request_id_var.set(rid)
    return rid


# ============================================================
# 脱敏：日志里不能出现密钥
# ============================================================

# 需要打码的内容（宁可多打一点，也不要漏出去）：
#   · 形如 sk-xxx / kbqa_xxx 的密钥
#   · Bearer 后面的令牌
#   · 长串的十六进制/大小写混合随机串（大概率是密钥或签名）
#   · 手机号（个人信息）
_SECRET_PATTERNS = [
    (re.compile(r"(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+"), r"\1***"),
    (re.compile(r"(kbqa_)[A-Za-z0-9_\-]{6,}"), r"\1***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***"),
    (re.compile(r"\b(1[3-9]\d)\d{4}(\d{4})\b"), r"\1****\2"),
    (re.compile(r"((?:password|passwd|pwd|secret|token|api_key)\"?\s*[:=]\s*[\"']?)[^\s\"',}]{6,}",
                re.IGNORECASE), r"\1***"),
]


def redact(text: str) -> str:
    """把日志文本里的敏感内容替换成 ***。

    ⚠️ 为什么要在**日志出口**统一做，而不是要求每个写日志的地方自己注意：
        靠人自觉是做不到的。只要有一处忘了，密钥就永久留在日志文件里了，
        而且不会有人发现。统一在出口拦一道，才是真的能守住。
    """
    out = str(text)
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


# ============================================================
# 格式化器
# ============================================================


class RequestIdFilter(logging.Filter):
    """给每条日志补上"请求编号"和"是哪个进程"两个字段。

    抽成 Filter 而不是在每个 logger 调用处手写：
    这样**所有**日志（包括第三方库打的）都自动带上，不用一处一处改。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        # 进程号很有用：多进程部署时，"是哪个进程出错"往往是第一条线索
        import os
        record.pid = os.getpid()
        return True


class HumanFormatter(logging.Formatter):
    """给人看的格式（默认）：时间 + 级别 + 请求编号 + 模块 + 内容。"""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} | {record.levelname:<5} | "
            f"[{getattr(record, 'request_id', '-')}] | {record.name} | "
        )
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        # 出口统一脱敏（见上面 redact 的说明）
        return base + redact(text)


class JsonFormatter(logging.Formatter):
    """给日志系统看的格式：一行一个 JSON 对象。

    为什么要它：日志系统（ELK/Loki 等）能直接按字段检索，
    比如"查所有 level=ERROR 且 path=/api/v1/chat 的日志"，
    不用写正则去猜。本机开发时 JSON 太难读，所以默认用文字格式。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "pid": getattr(record, "pid", 0),
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """配置根日志：级别、格式、脱敏与请求编号。

    幂等：重复调用不会叠加多个 handler（否则一条日志会被打印好几遍，
    而且每多调用一次就多打印一遍 —— 本项目真实见过"日志突然变成 3 倍"的排查现场）。
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))

    # 先把已有的 handler 清掉（幂等的关键）
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter() if (fmt or "text").lower() == "json" else HumanFormatter())
    root.addHandler(handler)

    # 顺带压一下几个话特别多的第三方库（它们会把日志刷得没法看）
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return None
