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
    """判断一行是不是 Markdown 表格的分隔行（|---|---|）。"""
    s = line.strip()
    if not s.startswith("|") or not s.endswith("|"):
        return False
    # 去掉首尾竖线后，剩下的应该都是 - : | 和空格
    inner = s.strip("|")
    if not inner:
        return False
    cells = [c.strip() for c in inner.split("|")]
    return all(c and set(c) <= set("-: ") for c in cells)


def _split_md_table(text: str) -> list[str]:
    """把 Markdown 表格按行拆开，每片都带上表头。

    Args:
        text: 可能含表格的文本。

    Returns:
        片段列表。没有表格时返回空列表（交给普通长文处理）。
    """
    lines = text.split(chr(10))

    # 找表头行：一行以 | 开头，且下一行是分隔行
    header_idx = None
    for i in range(len(lines) - 1):
        if (
            lines[i].strip().startswith("|")
            and lines[i].strip().endswith("|")
            and _is_md_table_separator(lines[i + 1])
        ):
            header_idx = i
            break

    if header_idx is None:
        return []

    header = lines[header_idx].rstrip()
    separator = lines[header_idx + 1].rstrip()
    table_prefix = header + chr(10) + separator

    # 收集数据行（到表格结束为止）
    data_rows: list[str] = []
    for ln in lines[header_idx + 2 :]:
        s = ln.strip()
        if s.startswith("|") and s.endswith("|"):
            data_rows.append(ln.rstrip())
        elif not s:
            # 空行：表格结束
            break
        else:
            # 非表格行：表格结束
            break

    if not data_rows:
        return []

    # 表头 + 分隔行本身也作为一个片段：
    # 用户可能问"这张表有哪些字段"，那时只需要表头
    pieces = [table_prefix]

    # 每行数据都带上表头，保证单独拿出来也能读懂
    for row in data_rows:
        piece = table_prefix + chr(10) + row
        # 单片超长时（列特别多）截断保底，避免远超 CHUNK_SIZE
        if len(piece) > CHUNK_SIZE * 2:
            piece = piece[: CHUNK_SIZE * 2]
        pieces.append(piece)

    return pieces


def _chunk_long_text(text: str, page: int | None = None, section: str | None = None) -> list[tuple[str, int | None, str | None]]:
    """把一段长文切成多片，返回 [(text, page, section)]。

    处理顺序：
      1. 如果这段里有 Markdown 表格 → 按「行 + 表头」切（见 _split_md_table）
      2. 否则按普通长文递归切

    为什么要先判表格：
        递归切分会按字符数硬切，把表格的行切到两个片段里。
        实测导致某一行关键数据落在边界上被切碎，
        检索命中文档但拿不到关键数字，模型只能回答"资料里没有"。
    """
    cleaned = _clean(text)
    if not cleaned:
        return []

    # 优先处理表格：每一行都成为一个可独立理解的片段
    table_pieces = _split_md_table(cleaned)
    if table_pieces:
        return [(p.strip(), page, section) for p in table_pieces if p.strip()]

    if len(cleaned) <= int(CHUNK_SIZE * 1.2):
        return [(cleaned, page, section)]
    splitter = _make_splitter()
    pieces = splitter.split_text(cleaned)
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
