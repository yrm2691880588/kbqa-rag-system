"""内容安全审核（本地敏感词库）。

为什么要有这个模块（大白话）：
    系统会用大模型生成回答。万一生成出违规内容，责任在平台这边——
    用户只看到"你们网站说了这句话"。所以必须有最后一道闸：
    上线前把明显不该出现的内容拦住，而不是等出事再解释。

为什么用"本地词库"而不是买在线审核服务：
    ① 免费、不花钱；② 不需要联网，私有化部署时客户内网也能用；
    ③ 词表是纯文本文件，客户可以自己加自己行业的词，不依赖任何供应商。
    代价是需要自己维护词表——所以词表放在 app/data/sensitive_words.txt，
    写成一行一个词、支持注释，运营人员用记事本就能改。

两道防线（都在这一个文件里，接口层只管调用）：
    · 用户提问（输入）  —— 挡住"拿我们的系统去生成违规内容"的人；
    · AI 回答（输出）   —— 平台担责的那一头，逐字检查，命中就掐断。

防绕过的关键设计：
    不要直接拿原文去比对词表——那样"赌 博"（中间加空格）、"賭博"（繁体）、
    全角字母、"ＧＡＭＢＬＥ" 这类写法全都躲得过去。所以匹配前先把文本
    "归一化"（全角转半角、去空白与常见分隔符、统一小写），
    并记下每个归一化字符来自原文的哪个位置，命中后再回到原文上打码。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("kbqa.content_filter")

# 词表文件位置：app/data/sensitive_words.txt
_WORDLIST_PATH = Path(__file__).resolve().parents[1] / "data" / "sensitive_words.txt"

# 打码时用的替代字符（默认用全角星号，看起来比半角明显）
_MASK_CHAR = "＊"


# ============================================================
# 文本归一化：把"花式写法"还原成正常写法，让词表比对不落空
# ============================================================

def _fullwidth_to_halfwidth(ch: str) -> str:
    """全角字符转半角（Ｇ→G、１→1、，→,）。非全角字符原样返回。"""
    code = ord(ch)
    if code == 0x3000:          # 全角空格
        return " "
    if 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII 区
        return chr(code - 0xFEE0)
    return ch


# 这些字符在"比对"时直接跳过：空白 + 常见的分隔/干扰符号。
# 跳过它们是为了让"赌  博"、"赌-博"、"赌*博"这类插入干扰的写法也能被认出来。
_SKIP_CHARS = set(" \t\r\n\u3000") | set("-_*./\\|+~^·.,;:!?()[]{}<>\"'`@#$%&=，。；：！？（）【】《》、…—‘’“”")


# 常见繁体字 → 简体字（只收"常被用来绕过审核"的高频字，不是完整繁简表）。
# 为什么需要：词库存的是简体（如"赌博"），但用户写"賭博"时字符并不相同，
# 直接比对会漏过去。归一化时先做这一步，繁体写法就能落到简体词条上。
# 覆盖范围说明：这里只做一对一单字映射，够挡住"换个繁体字写法"这类绕过；
# 真正的同义词/拼音变体需要语义级判断，不在本模块职责内。
_TRAD_TO_SIMP = str.maketrans({
    "賭": "赌", "場": "场", "錢": "钱", "貸": "贷", "貨": "货", "聯": "联",
    "絡": "络", "詐": "诈", "騙": "骗", "黨": "党", "槍": "枪", "彈": "弹",
    "藥": "药", "醫": "医", "療": "疗", "雜": "杂", "網": "网",
    "鏈": "链", "結": "结", "頁": "页", "點": "点", "擊": "击", "進": "进",
    "買": "买", "賣": "卖", "購": "购", "價": "价", "開": "开", "關": "关",
    "門": "门", "問": "问", "題": "题", "電": "电", "話": "话", "號": "号",
    "碼": "码", "掃": "扫", "視": "视", "頻": "频",
    "遊": "游", "戲": "戏", "娛": "娱", "樂": "乐", "團": "团", "隊": "队",
    "會": "会", "員": "员", "費": "费", "優": "优", "搶": "抢",
    "發": "发", "轉": "转", "賬": "账", "戶": "户", "銀": "银",
    "幣": "币", "虛": "虚", "擬": "拟", "贏": "赢", "輸": "输",
    "獎": "奖", "財": "财", "寶": "宝", "億": "亿", "萬": "万",
    "個": "个", "們": "们", "來": "来", "對": "对", "這": "这", "時": "时",
    "說": "说", "還": "还", "過": "过", "後": "后", "麼": "么", "樣": "样",
})


def _to_simplified(ch: str) -> str:
    """繁体字转简体（不在映射表里的原样返回）。"""
    return ch.translate(_TRAD_TO_SIMP)


def _build_scan_view(text: str) -> tuple[str, list[int]]:
    """生成用于比对的"归一化文本"，并记录每个字符对应原文的哪个位置。

    Returns:
        (归一化文本, 位置映射)
        位置映射[i] = 归一化文本第 i 个字符在**原文**里的下标。
        命中之后要靠它回到原文上打码（否则打码位置会错开）。
    """
    chars: list[str] = []
    positions: list[int] = []
    for idx, raw in enumerate(text):
        ch = _fullwidth_to_halfwidth(raw)
        if ch in _SKIP_CHARS:
            continue            # 干扰字符不参与比对
        ch = _to_simplified(ch)   # 繁体归一化成简体，避免「賭博」这类写法绕过
        chars.append(ch.lower())
        positions.append(idx)
    return "".join(chars), positions


# ============================================================
# 词表加载（带缓存 + 文件改动自动重载）
# ============================================================

@dataclass
class _WordList:
    """已加载的词表：词 + 每个词所属的分类（便于日志里说清"命中了哪类词"）。"""

    words: list[str] = field(default_factory=list)         # 原始词（用于展示）
    normalized: list[str] = field(default_factory=list)    # 归一化后的词（用于比对）
    categories: list[str] = field(default_factory=list)    # 与 words 一一对应
    mtime: float = 0.0
    path: str = ""                                          # 实际读取的文件（用于判断是否换了文件）


_cache = _WordList()


def _wordlist_path() -> Path:
    """决定读哪个词表文件。

    默认读内置的那份（app/data/sensitive_words.txt）；
    如果配置里填了自定义路径，就以配置为准 —— 这样不同客户可以各自维护自己的词表，
    不用去改代码目录里的文件（升级时也不会被覆盖掉）。
    """
    try:
        from app.core.config import settings
        custom = (getattr(settings, "content_filter_wordlist", "") or "").strip()
    except Exception:   # 配置读取失败也不能让审核功能把整个服务拖垮
        custom = ""
    if custom:
        p = Path(custom)
        return p if p.is_absolute() else Path.cwd() / p
    return _WORDLIST_PATH


def _parse_wordlist(raw_text: str) -> _WordList:
    """解析词表文本。

    格式（越简单越好，运营人员用记事本就能维护）：
        # 分类：广告导流          ← 以 # 开头是分类，不是词
        加微信                    ← 一行一个词
        加qq

    约定：分类行下面跟着的词都归到该分类；文件开头没写分类的，归到"未分类"。
    """
    words: list[str] = []
    normalized: list[str] = []
    categories: list[str] = []
    current = "未分类"

    for line in raw_text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            # 分类行：# 后面的内容作为分类名
            name = s.lstrip("#").strip()
            if name:
                current = name
            continue
        # 词本身也可能带干扰字符（比如写成"赌 博"），统一归一化后再入库
        norm, _ = _build_scan_view(s)
        if not norm:
            continue
        words.append(s)
        normalized.append(norm)
        categories.append(current)

    return _WordList(words=words, normalized=normalized, categories=categories)


def load_wordlist(force: bool = False) -> _WordList:
    """读取词表。文件没变就复用缓存；文件改了会自动重新加载。

    为什么要看 mtime：运营人员在服务器上直接改词表文件后，
    不应该还要重启服务才生效——这类"改了没生效"最容易让人怀疑"功能是不是坏的"。
    """
    global _cache
    path = _wordlist_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        if force or not _cache.words:
            logger.warning("敏感词表不存在或读不到：%s（将按「无词表」处理）", path)
        return _cache

    # 文件没变 且 还是同一个文件 → 直接用缓存
    if not force and _cache.words and _cache.mtime == mtime and _cache.path == str(path):
        return _cache

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("读取敏感词表失败（按无词表处理）：%s", exc)
        return _cache

    _cache = _parse_wordlist(text)
    _cache.mtime = mtime
    _cache.path = str(path)
    logger.info("敏感词表已加载：共 %d 个词，%d 个分类（%s）",
                len(_cache.words), len(set(_cache.categories)), path)
    return _cache


# ============================================================
# 检测结果
# ============================================================

@dataclass
class FilterHit:
    """一次命中：命中了哪个词、属于哪一类、在原文的什么位置。"""

    word: str
    category: str
    start: int   # 在**原文**中的起始下标
    end: int     # 在原文中的结束下标（不含）


@dataclass
class FilterResult:
    """审核结果。"""

    hit: bool                                   # 有没有命中
    hits: list[FilterHit] = field(default_factory=list)
    masked: str = ""                            # 打码后的文本（未命中时等于原文）

    @property
    def words(self) -> list[str]:
        """命中的词（去重）。"""
        return sorted({h.word for h in self.hits})

    @property
    def categories(self) -> list[str]:
        """命中的分类（去重）。"""
        return sorted({h.category for h in self.hits})


# ============================================================
# 检测与打码
# ============================================================

def check(text: str, mask: bool = True) -> FilterResult:
    """检查一段文本是否命中敏感词。

    Args:
        text: 待检查的原文。
        mask: 命中时是否顺带生成打码后的文本（打码位置会回到原文坐标）。

    Returns:
        FilterResult。未命中时 masked == 原文。
    """
    if not text:
        return FilterResult(hit=False, masked=text or "")

    wl = load_wordlist()
    if not wl.words:
        return FilterResult(hit=False, masked=text)

    view, positions = _build_scan_view(text)
    if not view:
        return FilterResult(hit=False, masked=text)

    hits: list[FilterHit] = []
    for word, norm, category in zip(wl.words, wl.normalized, wl.categories):
        # 必须循环找出**每一处**出现，不能只 find 一次。
        # 早期版本只取第一次出现，导致"加微信加微信"里第二处原样输出 ——
        # 与"打码时统一处理所有出现位置"的预期相反，等于漏放。
        start = view.find(norm)
        while start >= 0:
            end = start + len(norm) - 1
            hits.append(FilterHit(
                word=word,
                category=category,
                start=positions[start],
                end=positions[end] + 1,
            ))
            # 从本次命中之后继续找，避免死循环（步长至少 1）
            start = view.find(norm, start + 1)

    if not hits:
        return FilterResult(hit=False, masked=text)

    return FilterResult(hit=True, hits=hits, masked=mask_text(text, hits) if mask else text)


def mask_text(text: str, hits: list[FilterHit]) -> str:
    """把命中的片段替换成等长的星号。

    为什么要"等长"：如果直接删掉或换成固定长度的替代串，
    调用方（比如前端要做高亮、或者要算字数）会发现位置对不上。
    """
    if not hits:
        return text
    # 按位置从后往前替换，这样前面的下标不会因为替换而错位
    chars = list(text)
    for h in sorted(hits, key=lambda x: x.start, reverse=True):
        for i in range(h.start, min(h.end, len(chars))):
            chars[i] = _MASK_CHAR
    return "".join(chars)


def mask(text: str) -> str:
    """便捷函数：直接返回打码后的文本（没有词典/没命中时原样返回）。"""
    return check(text, mask=True).masked


# ============================================================
# 分片增量检测（给流式回答用）
# ============================================================

class StreamGuard:
    """流式回答的"逐段检查器"。

    难题：AI 的回答是一小段一小段吐出来的，一个敏感词可能正好被切成两半
    （前半段在上一块、后半段在这一块）。只看单块会漏掉。

    做法：每次拿到新片段，就把它接到缓冲区后面，然后**只检查缓冲区末尾
    一小段**（长度 = 最长敏感词的长度）。这样既不会漏掉跨块的词，
    也不用每次都把整篇回答重新扫一遍（回答越长越慢的问题就避免了）。

    用法：
        guard = StreamGuard()
        for piece in stream:
            hit = guard.feed(piece)
            if hit: ...   # 命中，立刻停止生成
    """

    def __init__(self, tail_extra: int = 0) -> None:
        self._buffer = ""
        self._checked_upto = 0     # 已经检查过的位置（避免重复检查）
        wl = load_wordlist()
        # 缓冲区需要保留的长度 = 最长敏感词的字符数（跨块最多跨这么多）
        self._keep = max((len(w) for w in wl.normalized), default=1) + max(0, tail_extra)

    def feed(self, piece: str) -> FilterResult:
        """喂入一段新文本，返回**这一段（含与上一段的接缝）**的检查结果。"""
        if not piece:
            return FilterResult(hit=False, masked="")
        self._buffer += piece

        # 只检查"上次检查位置 - 可能跨块的长度"之后的部分
        start = max(0, self._checked_upto - self._keep)
        window = self._buffer[start:]
        result = check(window)
        self._checked_upto = len(self._buffer)

        if result.hit:
            return result
        # 缓冲区只保留尾部一小段即可，避免长回答越积越大
        if len(self._buffer) > self._keep * 4:
            self._buffer = self._buffer[-self._keep:]
            self._checked_upto = len(self._buffer)
        return FilterResult(hit=False, masked="")

    @property
    def text(self) -> str:
        """目前收到的全部内容（用于日志/兜底展示）。"""
        return self._buffer


# ============================================================
# 给"用户提问"用的便捷判断
# ============================================================


def is_blocked_question(question: str) -> FilterResult:
    """判断用户提问是否应被拦截（只做检测，是否拦截由接口按配置决定）。"""
    return check(question, mask=False)
