"""智能切块：把解析出的文本切成适合检索与喂给模型的"知识片段"。

策略（针对电商资料场景）：
- Excel 商品表：**一行商品 = 一个片段**（整条记录不被拆散，检索可命中完整商品信息）；
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


def _chunk_long_text(text: str, page: int | None = None, section: str | None = None) -> list[tuple[str, int | None, str | None]]:
    """把一段长文切成多片，返回 [(text, page, section)]。"""
    cleaned = _clean(text)
    if not cleaned:
        return []
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
