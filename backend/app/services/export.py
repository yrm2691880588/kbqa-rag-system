"""会话记录导出：把一封对话导成 Excel。

## 为什么用 Excel 而不是 Markdown / PDF

用户的实际需求通常是"归档 + 统计"：

    · 这周问了多少个问题？
    · 哪些回答被踩了？为什么？
    · 平均响应时间多少？
    · 某类问题的引用来源集中在哪几份文档？

Markdown 和 PDF 只适合阅读，做不了上面这些。Excel 能排序、筛选、做透视表。

## 输出结构

一个工作表，每行一条**问答对**（不是一条消息）：

    序号 | 时间 | 提问 | 回答 | 引用来源 | 评价 | 评价原因 | 评价备注 | 响应耗时(ms)

为什么合并成"问答对"而不是"一问一行、一答一行"：
    用户看导出文件是为了回顾"问过什么、答得怎么样"。
    分开列的话，回答那一行的"提问"是空的，筛选时很难用。

## 为什么耗时只记在回答上

延迟是"这轮问答跑了多久"，属于问答对级别的信息，不是消息级别的。
"""
from __future__ import annotations

import datetime as dt
import io
import logging
from typing import Any

logger = logging.getLogger("kbqa.services.export")

# 表头。顺序就是导出文件的列顺序。
_HEADERS = [
    "序号",
    "时间",
    "提问",
    "回答",
    "引用来源",
    "评价",
    "评价原因",
    "评价备注",
    "响应耗时(ms)",
]

# 反馈值 -> 中文
_FEEDBACK_LABEL = {
    1: "👍 有帮助",
    -1: "👎 没帮助",
}


def _fmt_time(value: Any) -> str:
    """把时间格式化成「YYYY-MM-DD HH:MM:SS」（不带时区，Excel 里更好用）。"""
    if isinstance(value, dt.datetime):
        try:
            # 统一转成本地时间，避免用户看到 UTC 时间觉得"时间对不上"
            local = value.astimezone()
            return local.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


def _fmt_citations(citations: Any) -> str:
    """把引用列表压成一行摘要。

    格式：文件名（第N页）×2 → 表示这条回答引用了它两次
    为什么压缩：完整引用 JSON 会让单元格变成几千字，Excel 里根本没法看。
    需要细节时用户可以点开原会话。
    """
    if not citations:
        return ""

    counter: dict[str, int] = {}
    for c in citations:
        if not isinstance(c, dict):
            continue
        name = str(c.get("doc_name") or "").strip() or "（未命名资料）"
        page = c.get("page")
        key = f"{name}（第 {page} 页）" if page else name
        counter[key] = counter.get(key, 0) + 1

    if not counter:
        return ""
    # 按出现次数降序，出现多次的排前面
    parts = [
        f"{k} ×{v}" if v > 1 else k
        for k, v in sorted(counter.items(), key=lambda kv: -kv[1])
    ]
    return "；".join(parts)


def build_session_xlsx(
    session_title: str,
    messages: list[dict],
    *,
    exported_at: dt.datetime | None = None,
) -> bytes:
    """把一封会话的消息列表导出成 Excel 文件的字节内容。

    Args:
        session_title: 会话标题（写进文件属性，方便区分）。
        messages: 消息字典列表，按时间升序。每项需含：
            role / content / created_at / citations / feedback /
            feedback_reason / feedback_note / latency_ms
        exported_at: 导出时间（默认当前时间）。

    Returns:
        xlsx 文件的字节内容。调用方直接当 HTTP 响应体返回。

    Note:
        这个函数**不抛异常给上层**：导出失败时返回一个只含错误说明的
        Excel，让用户至少能打开看一眼，而不是收到一个 500。
    """
    try:
        return _build(session_title, messages, exported_at)
    except Exception as exc:  # noqa: BLE001
        logger.warning("导出会话失败：%s", exc)
        return _build_error(str(exc))


def _build_error(reason: str) -> bytes:
    """生成一个"导出失败"的 Excel（而不是让请求 500）。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "导出失败"
    ws.append(["导出失败"])
    ws.append(["原因", reason])
    ws.append(["建议", "请重试；如果一直失败，请联系管理员查看服务端日志。"])
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 80

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build(
    session_title: str,
    messages: list[dict],
    exported_at: dt.datetime | None,
) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "对话记录"

    # ---- 表头 ----
    ws.append(_HEADERS)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4472C4")
    for col_idx in range(1, len(_HEADERS) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # ---- 数据：把消息配成问答对 ----
    #
    # 配对规则：遇到 user 就记下"当前提问"，遇到 assistant 就输出一行。
    # 为什么这样配：正常流程一定是 user → assistant。
    # 极少数情况（中断/失败）可能只有 user 没有 assistant，
    # 那种情况也要输出一行（回答列留空），否则用户会以为问题丢了。
    pending_q: dict | None = None
    row_no = 0

    for m in messages:
        role = str(m.get("role") or "")
        if role == "user":
            # 上一个提问还没等到回答 → 先把这行补出去
            if pending_q is not None:
                row_no += 1
                ws.append(
                    [
                        row_no,
                        _fmt_time(pending_q.get("created_at")),
                        pending_q.get("content") or "",
                        "（本轮未生成回答）",
                        "",
                        "",
                        "",
                        "",
                        "",
                    ]
                )
            pending_q = m
            continue

        if role != "assistant":
            continue

        row_no += 1
        q = pending_q or {}
        fb = m.get("feedback")
        ws.append(
            [
                row_no,
                _fmt_time(m.get("created_at") or q.get("created_at")),
                q.get("content") or "",
                m.get("content") or "",
                _fmt_citations(m.get("citations")),
                _FEEDBACK_LABEL.get(fb, "") if fb is not None else "",
                m.get("feedback_reason") or "",
                m.get("feedback_note") or "",
                m.get("latency_ms") if m.get("latency_ms") is not None else "",
            ]
        )
        pending_q = None

    # 结尾还剩一个没被回答的提问
    if pending_q is not None:
        row_no += 1
        ws.append(
            [
                row_no,
                _fmt_time(pending_q.get("created_at")),
                pending_q.get("content") or "",
                "（本轮未生成回答）",
                "",
                "",
                "",
                "",
                "",
            ]
        )

    # ---- 列宽与换行 ----
    #
    # 为什么这几列要给固定宽度 + 自动换行：
    #   openpyxl 默认列宽很窄，长文本会被挤成一行看不到。
    #   设了宽度和 wrap_text 后，用户打开就能看到完整内容。
    widths = {
        "序号": 6,
        "时间": 20,
        "提问": 40,
        "回答": 60,
        "引用来源": 40,
        "评价": 12,
        "评价原因": 14,
        "评价备注": 24,
        "响应耗时(ms)": 14,
    }
    for idx, name in enumerate(_HEADERS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = widths.get(name, 16)

    # 长文本列允许换行（序号/时间/耗时这类不需要）
    wrap_cols = {3, 4, 5, 8}
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for cell in row:
            if cell.column in wrap_cols:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            else:
                cell.alignment = Alignment(vertical="top")

    # 冻结表头：滚动时表头始终可见
    ws.freeze_panes = "A2"

    # ---- 文件属性 ----
    when = (exported_at or dt.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    wb.properties.title = f"{session_title} - 对话记录"
    wb.properties.subject = f"导出于 {when}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def suggest_filename(session_title: str, session_id: int) -> str:
    """生成一个安全的导出文件名。

    Args:
        session_title: 会话标题（用户可能写中文、符号、超长）。
        session_id: 会话 id（兜底保证唯一）。

    Returns:
        形如「对话记录_某标题_20261118.xlsx」的文件名。

    Note:
        去除 Windows/Unix 都不允许的字符，并限制长度 ——
        用户给会话起名叫 `a/b:c*d?.md` 时不能生成非法文件名。
    """
    bad = set('\\/:*?"<>|' + chr(10) + chr(13) + chr(9))
    safe = "".join(ch for ch in (session_title or "") if ch not in bad).strip()
    safe = safe[:40] or f"会话{session_id}"
    stamp = dt.date.today().strftime("%Y%m%d")
    return f"对话记录_{safe}_{stamp}.xlsx"
