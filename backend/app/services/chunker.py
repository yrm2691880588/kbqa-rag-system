"""智能切块：把解析出的文本切成适合检索与喂给模型的"知识片段"。

策略（针对文档资料场景）：
- Excel 表格：**一行记录 = 一个片段**（整条记录不被拆散，检索可命中完整信息）；
- PDF/Word/网页长文：按语义断点递归切分（段落 > 句子 > 标点），片段间保留少量重叠，
  防止跨片段的问题被截断丢失上下文；
- 每个片段记录来源页码（PDF）或工作表位置（Excel），供回答时引用展示。
"""

import logging
import re
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.services.file_parser import ParseResult, ParsedUnit

logger = logging.getLogger("kbqa.chunker")

# 片段目标长度（字符）：中文场景 500 字左右信息密度适中，检索与模型上下文都友好
CHUNK_SIZE = 500
# 相邻片段重叠长度：防止句子在边界被切断导致语义丢失
CHUNK_OVERLAP = 80


@dataclass
class Chunk:
    index: int
    text: str
    page: int | None = None
    section: str | None = None


def _clean(text: str) -> str:
    """清洗：压缩多余空白/空行，去掉只含标点的行。"""
    text = re.sub(r"[ \t\u3000]+", " ", text)  # 连续空格/全角空格合并为单个空格
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not re.fullmatch(r"[\s\-—_=*#·•]+", ln)]
    text = "\n".join(lines)
    return text.strip()


def _make_splitter() -> RecursiveCharacterTextSplitter:
    """中文断点优先：段 → 句 → 分句 → 词 → 字。"""
    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
        keep_separator="end",
    )


def _is_md_table_separator(line: str) -> bool:
    """判断一行是不是 Markdown 表格的分隔行。

    支持两种写法：
        |---|---|---|      （有外侧竖线）
        --- | --- | ---    （省略外侧竖线，Markdown 规范允许）
    """
    s = line.strip()
    if "-" not in s:
        return False
    # 去掉首尾竖线后按 | 分格
    inner = s.strip("|")
    if not inner:
        return False
    cells = [c.strip() for c in inner.split("|")]
    if not cells:
        return False
    # 每一格都必须只由 - : 空格 组成，且至少有一个 -
    for c in cells:
        if not c or set(c) - set("-: "):
            return False
    return True


def _is_table_row(line: str) -> bool:
    """判断一行是不是表格数据行。

    放宽到"含 | 即可"，因为 Markdown 允许省略外侧竖线。
    """
    s = line.strip()
    return "|" in s and not s.startswith("#")


def _split_into_segments(text: str) -> list[tuple[str, str]]:
    """把文本切成 [(类型, 内容)]，类型是 "text" 或 "table"。

    Args:
        text: 已清洗的文本。

    Returns:
        段落列表，按原顺序。表格段落包含表头 + 分隔行 + 数据行。

    Note:
        这一步是修复"表格吞掉正文"的关键：
            上一版发现表格就整段返回，把表前/表后的正文全丢了
            （实测真实文档丢了 68%）。
        现在先把文档拆成"表 / 非表"交替的段，各段分别处理。
    """
    lines = text.split(chr(10))
    segments: list[tuple[str, str]] = []

    buf: list[str] = []          # 普通文本缓冲
    i = 0
    n = len(lines)

    def flush_text() -> None:
        if buf:
            joined = chr(10).join(buf).strip()
            if joined:
                segments.append(("text", joined))
            buf.clear()

    while i < n:
        line = lines[i]

        # 判断当前位置是否是一个表格的开头（表头行 + 下一行是分隔行）
        is_table_start = (
            _is_table_row(line)
            and i + 1 < n
            and _is_md_table_separator(lines[i + 1])
        )

        if not is_table_start:
            buf.append(line)
            i += 1
            continue

        # 遇到表格：先把之前的正文冲出去
        flush_text()

        header = line.rstrip()
        separator = lines[i + 1].rstrip()
        table_lines = [header, separator]
        i += 2

        # 收集数据行，直到遇到非表格行
        # 注意：连续的多个表格会被 _clean 删掉空行后粘在一起，
        # 所以要能识别"又一个表头"并断开。
        while i < n:
            cur = lines[i]
            if not _is_table_row(cur):
                break
            # 又出现"表头 + 分隔行"→ 说明进入了下一张表，当前表结束
            if (
                i + 1 < n
                and _is_md_table_separator(lines[i + 1])
            ):
                break
            table_lines.append(cur.rstrip())
            i += 1

        segments.append(("table", chr(10).join(table_lines)))

    flush_text()
    return segments


def _split_md_table(text: str) -> list[str]:
    """把文本里的**第一个**表格按行拆开，每片带表头。

    保留这个函数是为了兼容既有测试与调用方。
    实际切分走 _split_into_segments（能处理多表 + 正文）。
    """
    for kind, content in _split_into_segments(text):
        if kind != "table":
            continue

        lines = content.split(chr(10))
        if len(lines) < 2:
            return []
        header = lines[0].rstrip()
        separator = lines[1].rstrip()
        table_prefix = header + chr(10) + separator
        data_rows = lines[2:]

        if not data_rows:
            return []

        pieces = [table_prefix]
        for row in data_rows:
            pieces.append(table_prefix + chr(10) + row)
        return pieces

    return []


def _chunk_table(content: str) -> list[str]:
    """把一张表格切成"每行 + 表头"的片段。"""
    lines = [ln for ln in content.split(chr(10)) if ln.strip()]
    if len(lines) < 2:
        return []

    header = lines[0]
    separator = lines[1]
    prefix = header + chr(10) + separator
    rows = lines[2:]

    # 只有表头没有数据行：直接把表头作为一片（用户可能问"有哪些字段"）
    if not rows:
        return [prefix]

    pieces = []
    for row in rows:
        piece = prefix + chr(10) + row
        if len(piece) > CHUNK_SIZE * 2:
            piece = piece[: CHUNK_SIZE * 2]
        pieces.append(piece)
    return pieces


def _chunk_long_text(
    text: str, page: int | None = None, section: str | None = None
) -> list[tuple[str, int | None, str | None]]:
    """把一段文本切成多片，返回 [(text, page, section)]。

    处理方式：
      1. 先按表格把文本拆成"表 / 非表"交替的段
      2. 表格段 → 按「行 + 表头」切（每片带表头）
      3. 非表段 → 按普通长文递归切

    ⚠️ 绝对不能"发现表格就整段返回"：
        上一版就是这么写的，实测把真实文档的 68% 内容全丢了
        （表前正文、表后正文、后续其它表格全没了）。
        用户问那些内容会得到"资料里没有"，比修复前更糟。

    Args:
        text: 待切分的文本。
        page: 页码（随片段保留）。
        section: 位置说明。

    Returns:
        片段列表。
    """
    cleaned = _clean(text)
    if not cleaned:
        return []

    segments = _split_into_segments(cleaned)

    # 没有表格 → 走普通长文切分
    if not any(kind == "table" for kind, _ in segments):
        return _chunk_plain(cleaned, page, section)

    out: list[tuple[str, int | None, str | None]] = []
    for kind, content in segments:
        if kind == "table":
            for piece in _chunk_table(content):
                out.append((piece.strip(), page, section))
        else:
            out.extend(_chunk_plain(content, page, section))
    return out


def _chunk_plain(
    text: str, page: int | None = None, section: str | None = None
) -> list[tuple[str, int | None, str | None]]:
    """普通长文的递归切分（原 _chunk_long_text 的逻辑）。"""
    if not text:
        return []
    if len(text) <= int(CHUNK_SIZE * 1.2):
        return [(text, page, section)]

    splitter = _make_splitter()
    pieces = splitter.split_text(text)
    return [(p.strip(), page, section) for p in pieces if p and p.strip()]


def chunk_parse_result(result: ParseResult, doc_name: str) -> list[Chunk]:
    """把解析结果切成带序号的片段列表。"""
    out: list[Chunk] = []
    for unit in result.units:
        if result.kind == "table":
            # 表格行：整行一个片段（超长再切）
            pieces = _chunk_long_text(unit.text, unit.page, unit.section)
            for text, page, section in pieces:
                out.append(Chunk(index=0, text=text, page=page, section=section))
        else:
            # 长文：按页单元切，页码随片段保留
            pieces = _chunk_long_text(unit.text, unit.page, unit.section)
            for text, page, section in pieces:
                out.append(Chunk(index=0, text=text, page=page, section=section))
    # 统一编号
    for i, c in enumerate(out):
        c.index = i
    return out
