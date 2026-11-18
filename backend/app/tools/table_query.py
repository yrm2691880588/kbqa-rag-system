"""表格结构化查询工具：查 Excel 里的精确数据。

## 为什么需要这个工具

向量检索对"表格里的一行"区分度很低。实测两个典型失败：

    「某公司 2025 年前三季度的营业收入合计是多少？」
      → 需要把 Q1+Q2+Q3 加起来，检索做不到"聚合"

    「哪个对象的人数最多？」
      → 答案 `8642` 是纯数字，Qdrant 全文索引匹配不到纯数字

这两个都不是"检索不准"，而是**检索这种手段本身做不到**。

## 做法：让模型直接查原始 Excel

    模型判断"问题需要精确表格数据"
      → 调用本工具
      → 工具读原始 xlsx，做筛选/求和
      → 返回精确结果

好处：
  · 不改动入库流程（现有向量检索照常工作）
  · 数据永远是最新的（直接读用户上传的文件）
  · 能做聚合（求和、筛选），这是检索做不到的

## ⚠️ 关于"工具必须是纯函数"这条约定

`base.py` 顶部写了三条安全约定，第 2 条是"当前所有工具都是纯函数，
不碰数据库、不碰文件、不联网"。

**本工具是明确的例外**，理由和边界：

  · 只**读**文件，不写、不删、不改
  · 只读**当前知识库范围内**、由调用方显式传入的文档
  · 文件路径由固定的 `doc_file_path(kb_id, doc_id, ext)` 生成，
    不接受模型传入的任意路径（防路径穿越）
  · 模型只能传"知识库 id / 文档 id / 表名 / 筛选条件"，
    这些都由本工具校验后才使用

所以接口上通过 `needs_context` 标记显式声明"我需要调用方提供上下文"，
而不是偷偷放宽约定。rag.py 看到这个标记才会传上下文进来。

## 参数设计：为什么不做成 Text-to-SQL

Text-to-SQL 看起来更通用，但：
  · 模型生成的 SQL 可能写错（表名、列名、语法）
  · 出错时的错误信息对模型不友好，很难自我纠正
  · 需要严格沙箱（防止 DROP/UPDATE）

本工具只暴露四个受限操作（列表/看列/筛选/求和），
模型不会"写错语法"——它只能从固定选项里选。
代价是灵活性低一些，但正确率高得多。
"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.base import ToolResult

logger = logging.getLogger("kbqa.tools.table_query")

# 单次查询最多返回多少行（防止模型一次拉出几千行把上下文撑爆）
_MAX_ROWS = 50

# 允许聚合的列类型判定用不到，这里只做字符串/数字的粗分
_NUMERIC_HINT = ("收入", "利润", "资产", "负债", "金额", "预算", "支出",
                 "人数", "增速", "率", "ROE", "ROA", "代码", "数")


def _looks_numeric(value: str) -> bool:
    """判断单元格是不是数字（允许带 % 和千分位逗号）。"""
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def _to_number(value: str) -> float | None:
    """转成数字；转不了返回 None。"""
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fmt(num: float) -> str:
    """数字格式化：整数不带小数点，小数保留合理位数。"""
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.4f}".rstrip("0").rstrip(".")


class TableQueryTool:
    """查询 Excel 表格里的精确数据。

    ## 能力边界（刻意做窄）

        支持：列出工作表、查看列名、按条件筛选行、对某列求和
        不支持：任意 SQL、跨文件关联、写入

    ## 为什么做窄

        模型能用错的自由度越小，正确率越高。
        Text-to-SQL 看起来强大，但模型写错表名/语法时很难自我纠正。
        这里每个操作都是"从固定选项里选"，不会语法错误。
    """

    name = "table_query"

    description = (
        "查询 Excel 表格里的精确数据。当问题涉及以下情况时使用："
        "① 需要具体数字（某个指标的值、人数、金额）；"
        "② 需要把多个数值加起来（如「前三季度营收总和」）；"
        "③ 需要按条件筛选（如「某个部门的预算执行率」）；"
        "④ 需要比较多个对象的同一个指标。"
        "如果问题是要理解概念、找制度条款、看文字说明，不要调用本工具，"
        "直接用知识库资料回答即可。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_tables", "describe", "query", "sum"],
                "description": (
                    "list_tables: 列出所有表格；"
                    "describe: 查看某张表的列名；"
                    "query: 按条件筛选行；"
                    "sum: 对筛选出来的行求某列总和"
                ),
            },
            "table": {
                "type": "string",
                "description": "工作表名（先用 list_tables 查）。action=describe/query/sum 时必填。",
            },
            "filters": {
                "type": "object",
                "description": (
                    '筛选条件，形如 {"公司名称": "某公司", "期间": ["Q1","Q2"]}。'
                    "值是字符串表示精确匹配，是数组表示「命中任一即可」。"
                    "不传则不过滤（返回全部行，最多 50 行）。"
                ),
            },
            "column": {
                "type": "string",
                "description": "action=sum 时必填，要对哪一列求和（列名要完全一致）。",
            },
        },
        "required": ["action"],
    }

    # 标记：这个工具需要调用方提供上下文（知识库里的表格文件列表）。
    #
    # 为什么用类属性而不是参数：参数是给**模型**填的，
    # 而"有哪些文件"是调用方（rag.py）已知的信息，不该让模型去猜 id。
    needs_context = True

    async def run(self, **kwargs: Any) -> ToolResult:
        """执行查询。

        Args:
            **kwargs: 由模型填写（action/table/filters/column），
                外加调用方注入的 `_context`（不是模型能控制的）。

        Returns:
            ToolResult。
        """
        action = str(kwargs.get("action") or "").strip()
        if not action:
            return ToolResult("缺少 action 参数", ok=False)

        # 调用方注入的上下文：可用表格文件列表
        #
        # 结构：[{"kb_id": 3, "doc_id": 7, "name": "08_财务状况数据表.xlsx"}]
        context = kwargs.get("_context") or {}
        tables = context.get("tables") or []

        if not tables:
            return ToolResult(
                "当前知识库里没有可查询的 Excel 表格文件。"
                "请直接用检索到的资料回答，并说明数据来源。",
                ok=False,
            )

        try:
            if action == "list_tables":
                return self._list_tables(tables)
            if action == "describe":
                return self._describe(tables, str(kwargs.get("table") or ""))
            if action in ("query", "sum"):
                return self._query(
                    tables,
                    str(kwargs.get("table") or ""),
                    kwargs.get("filters") or {},
                    str(kwargs.get("column") or ""),
                    do_sum=(action == "sum"),
                )
            return ToolResult(
                f"不支持的 action：{action}（只支持 list_tables/describe/query/sum）",
                ok=False,
            )
        except Exception as exc:  # noqa: BLE001
            # 约定：工具失败不抛异常，返回错误文本让模型自己组织回答
            logger.warning("表格查询失败：%s", exc)
            return ToolResult(f"查询出错：{type(exc).__name__}: {exc}", ok=False)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _open(self, entry: dict):
        """打开一个 Excel 文件，返回 (workbook, 文件路径)。"""
        import openpyxl

        from app.services.ingestion import doc_file_path

        path = doc_file_path(int(entry["kb_id"]), int(entry["doc_id"]), "xlsx")
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{entry.get('name')}")
        # data_only=True：读公式的计算结果，而不是公式本身
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        return wb, path

    def _read_sheet(self, ws) -> tuple[list[str], list[list[str]]]:
        """读一张表，返回 (表头, 数据行)。

        ## 为什么要过滤"尾部说明行"

            真实 Excel 常在数据末尾写几行备注，例如：
                说明：ROE 与 ROA 均用平均余额计算。
                银行资产负债率 90% 以上属行业正常水平...

            这些行的特点是：只有第一列有内容，其余列全空。
            不去掉会导致：
              · 求和时把它们当成数据行（虽然数字转不了会被跳过，
                但"参与求和的 3 行"里会混进它们，模型描述时会困惑）
              · 展示时表格看起来有个残缺的尾巴

            判定：整行只有第 1 个单元格非空 → 视为备注，跳过。
            这个规则不完美（如果数据本身只有一列，会误杀），
            但财务表格至少 3~4 列，误杀风险很低。
            为了安全，只在**表头列数 >= 3** 时才启用这个过滤。
        """
        header: list[str] = []
        rows: list[list[str]] = []
        for values in ws.iter_rows(values_only=True):
            vals = [str(v).strip() if v is not None else "" for v in values]
            if not any(vals):
                continue
            if not header:
                header = vals
                continue

            # 尾部备注行过滤（仅多列表格启用）
            if len(header) >= 3:
                non_empty = sum(1 for v in vals if v)
                if non_empty == 1 and not vals[0][0].isdigit():
                    # 只有一个非空单元格，且不是纯数字 → 备注行
                    continue

            rows.append(vals)
        return header, rows

    def _list_tables(self, tables: list[dict]) -> ToolResult:
        """列出所有可用的工作表。"""
        lines: list[str] = []
        for entry in tables:
            try:
                wb, _ = self._open(entry)
                names = [ws.title for ws in wb.worksheets]
                wb.close()
                lines.append(f"文件《{entry.get('name')}》包含工作表：")
                for n in names:
                    lines.append(f"  - {n}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"文件《{entry.get('name')}》读取失败：{exc}")

        if not lines:
            return ToolResult("没有可读取的表格文件", ok=False)
        return ToolResult("\n".join(lines), ok=True)

    def _describe(self, tables: list[dict], table_name: str) -> ToolResult:
        """查看某张表的列名和数据规模。"""
        if not table_name:
            return ToolResult("缺少 table 参数", ok=False)

        for entry in tables:
            try:
                wb, _ = self._open(entry)
                if table_name not in [ws.title for ws in wb.worksheets]:
                    wb.close()
                    continue
                ws = wb[table_name]
                header, rows = self._read_sheet(ws)
                wb.close()
                cols = "、".join(h or f"列{i+1}" for i, h in enumerate(header))
                return ToolResult(
                    f"工作表《{table_name}》（来自《{entry.get('name')}》）\n"
                    f"共 {len(rows)} 行数据\n"
                    f"列名：{cols}",
                    ok=True,
                )
            except Exception:  # noqa: BLE001
                continue

        return ToolResult(f"找不到工作表《{table_name}》，请先用 list_tables 查看", ok=False)

    def _query(
        self,
        tables: list[dict],
        table_name: str,
        filters: dict,
        column: str,
        do_sum: bool,
    ) -> ToolResult:
        """按条件筛选行；do_sum 时对该列求和。"""
        if not table_name:
            return ToolResult("缺少 table 参数", ok=False)

        for entry in tables:
            try:
                wb, _ = self._open(entry)
                if table_name not in [ws.title for ws in wb.worksheets]:
                    wb.close()
                    continue
                ws = wb[table_name]
                header, rows = self._read_sheet(ws)
                wb.close()

                idx = {h: i for i, h in enumerate(header) if h}
                matched = self._filter_rows(header, rows, filters, idx)

                if not matched:
                    return ToolResult(
                        f"在《{table_name}》里没有符合条件 {filters} 的行。"
                        f"可用的列名：{'、'.join(header)}",
                        ok=False,
                    )

                if do_sum:
                    return self._do_sum(table_name, column, idx, matched, filters)

                return self._format_rows(table_name, header, matched, filters)
            except Exception:  # noqa: BLE001
                continue

        return ToolResult(f"找不到工作表《{table_name}》，请先用 list_tables 查看", ok=False)

    def _filter_rows(
        self,
        header: list[str],
        rows: list[list[str]],
        filters: dict,
        idx: dict,
    ) -> list[list[str]]:
        """按条件筛选。

        支持两种条件值：
          字符串 → 精确匹配（去掉首尾空白）
          数组   → 命中任一即可
        """
        out: list[list[str]] = []
        for row in rows:
            ok = True
            for col, want in (filters or {}).items():
                i = idx.get(col)
                if i is None or i >= len(row):
                    # 列名写错时不做过滤（而不是筛掉全部），
                    # 这样模型至少还能拿到数据，不会因为列名笔误一无所获
                    continue
                actual = row[i].strip()
                if isinstance(want, (list, tuple)):
                    if not any(actual == str(w).strip() for w in want):
                        ok = False
                        break
                else:
                    if actual != str(want).strip():
                        ok = False
                        break
            if ok:
                out.append(row)
        return out

    def _format_rows(
        self,
        table_name: str,
        header: list[str],
        rows: list[list[str]],
        filters: dict,
    ) -> ToolResult:
        """把筛选结果格式化成表格文本。"""
        shown = rows[:_MAX_ROWS]
        lines = [
            f"工作表《{table_name}》筛选结果（条件：{filters or '无'}）",
            f"共 {len(rows)} 行" + (f"，显示前 {len(shown)} 行" if len(rows) > len(shown) else ""),
            "",
        ]
        # 用竖线分隔，模型和人都好读
        lines.append(" | ".join(header))
        lines.append("-" * 40)
        for row in shown:
            padded = row + [""] * (len(header) - len(row))
            lines.append(" | ".join(padded[: len(header)]))
        return ToolResult("\n".join(lines), ok=True)

    def _do_sum(
        self,
        table_name: str,
        column: str,
        idx: dict,
        rows: list[list[str]],
        filters: dict,
    ) -> ToolResult:
        """对指定列求和，并列出参与求和的明细（便于模型核对）。"""
        if not column:
            return ToolResult("action=sum 时必须提供 column 参数", ok=False)

        i = idx.get(column)
        if i is None:
            return ToolResult(
                f"找不到列《{column}》。可用的列名：{'、'.join(idx.keys())}",
                ok=False,
            )

        total = 0.0
        used: list[str] = []
        skipped = 0
        for row in rows:
            v = row[i] if i < len(row) else ""
            num = _to_number(v)
            if num is None:
                if str(v).strip():
                    skipped += 1
                continue
            total += num
            # 明细里带上"行标识"（第一个非空列），方便模型描述是哪些行
            label = next((c for c in row if str(c).strip()), "")
            used.append(f"{label}={v}")

        if not used:
            return ToolResult(
                f"列《{column}》在筛选出的 {len(rows)} 行里没有可求和的数值"
                + (f"（{skipped} 个非数字值被跳过）" if skipped else ""),
                ok=False,
            )

        detail = " + ".join(used) if len(used) <= 12 else f"{len(used)} 个数值"
        note = f"\n（{skipped} 个非数字值被跳过）" if skipped else ""

        return ToolResult(
            f"求和结果：《{table_name}》的「{column}」\n"
            f"筛选条件：{filters or '无'}\n"
            f"参与求和：{len(used)} 行 —— {detail}\n"
            f"合计 = {_fmt(total)}{note}",
            ok=True,
            meta={"total": total, "count": len(used), "column": column},
        )
