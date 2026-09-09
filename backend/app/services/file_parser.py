"""多格式文档解析：把上传文件的内容抽成干净的文本单元，供后续切块。

支持格式：docx / pdf / xlsx / txt / md / html
输出统一结构 ParsedUnit：一段带来源位置信息的文本。
- 对"连贯长文"（docx/txt/md/html/pdf 正文），按页或整篇产出单元，交给切块器递归切分；
- 对 Excel 表格，**一行商品 = 一个单元**（电商场景关键：保证检索能命中整条商品记录）。
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("kbqa.parser")

# 允许上传的文件扩展名（小写）
ALLOWED_EXTENSIONS = {"docx", "pdf", "xlsx", "txt", "md", "html", "htm"}


@dataclass
class ParsedUnit:
    text: str
    page: int | None = None      # 页码（PDF 从 1 起），其他格式为 None
    section: str | None = None   # 位置说明（Excel 的 工作表+行号）


@dataclass
class ParseResult:
    kind: str                 # "flow"=连贯长文(需递归切块) / "table"=按行成块(每行已是独立单元)
    units: list[ParsedUnit]
    warnings: list[str]


def normalize_ext(filename: str) -> str | None:
    """取扩展名并校验是否支持；不支持返回 None。"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext if ext in ALLOWED_EXTENSIONS else None


# ---------------------------------------------------------------- docx

def _parse_docx(path: str) -> ParseResult:
    import docx  # python-docx

    doc = docx.Document(path)
    parts: list[str] = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if t:
            parts.append(t)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            # 去重合并单元格造成的重复列
            clean: list[str] = []
            for c in cells:
                if not clean or clean[-1] != c:
                    clean.append(c)
            line = " | ".join(x for x in clean if x)
            if line:
                parts.append(line)
    text = "\n\n".join(parts)
    return ParseResult(kind="flow", units=[ParsedUnit(text=text)] if text else [], warnings=[])


# ---------------------------------------------------------------- pdf

def _parse_pdf(path: str) -> ParseResult:
    import pymupdf as fitz  # PyMuPDF（新版模块名；旧版为 fitz。本行兼容"pymupdf"和"fitz"两种命名）

    warnings: list[str] = []
    units: list[ParsedUnit] = []
    doc = fitz.open(path)
    blank_pages = 0
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text").strip()
        if not text:
            blank_pages += 1
            continue
        units.append(ParsedUnit(text=text, page=i))
    doc.close()
    if blank_pages:
        warnings.append(f"PDF 中有 {blank_pages} 页未提取到文字（可能是扫描图片页，需要 OCR 才能读取）")
    if not units:
        warnings.append("该 PDF 未能提取到任何文字，请确认不是纯图片/扫描件")
    return ParseResult(kind="flow", units=units, warnings=warnings)


# ---------------------------------------------------------------- xlsx

def _parse_xlsx(path: str) -> ParseResult:
    import openpyxl

    warnings: list[str] = []
    units: list[ParsedUnit] = []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header: list[str] = []
        row_no = 0
        for values in rows:
            row_no += 1
            # 该行任一单元格有内容才算有效行
            if not any(v is not None and str(v).strip() for v in values):
                continue
            vals = [str(v).strip() if v is not None else "" for v in values]
            if not header:
                # 第一行有效数据当作表头
                header = vals
                continue
            # 表头缺失时用"列1"兜底，避免 "None: 值"
            labels = [h if h else f"列{i+1}" for i, h in enumerate(header)]
            pairs = []
            for i, v in enumerate(vals):
                if v:
                    pairs.append(f"{labels[i]}:{v}" if i < len(labels) else v)
            line = "；".join(pairs)
            if line:
                units.append(ParsedUnit(text=line, section=f"{ws.title} 第{row_no}行"))
    wb.close()
    if not units:
        warnings.append("Excel 中没有解析到数据行（请确认首行为表头、后续行为数据）")
    return ParseResult(kind="table", units=units, warnings=warnings)


# ---------------------------------------------------------------- txt / md / html

def _read_text_with_fallback(path: str) -> str:
    """按 UTF-8 优先读取文本文件，失败时回退 GBK（Windows 常见编码）。"""
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _parse_plain(path: str) -> ParseResult:
    text = _read_text_with_fallback(path)
    text = re.sub(r"\r\n?", "\n", text).strip()
    return ParseResult(kind="flow", units=[ParsedUnit(text=text)] if text else [], warnings=[])


def _parse_html(path: str) -> ParseResult:
    from bs4 import BeautifulSoup

    html = _read_text_with_fallback(path)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return ParseResult(kind="flow", units=[ParsedUnit(text=text)] if text else [], warnings=[])


# ---------------------------------------------------------------- 入口

def parse_document(path: str, doc_type: str) -> ParseResult:
    """根据类型分发到对应解析器。doc_type 为已归一化的小写扩展名。"""
    if doc_type == "docx":
        return _parse_docx(path)
    if doc_type == "pdf":
        return _parse_pdf(path)
    if doc_type == "xlsx":
        return _parse_xlsx(path)
    if doc_type in ("html", "htm"):
        return _parse_html(path)
    return _parse_plain(path)  # txt / md 及其他文本类
