"""数据清理与归档：让长期运行的数据库不越跑越胖。

为什么需要（大白话）：
    有两类数据只会增加、永远不会自己减少：
      · 问答日志（每次提问记一条）
      · 过期的验证码、失效的登录令牌
    一家公司一天问 500 次，一年就是 18 万条日志；验证码更是每发一次留一条。
    时间长了数据库会变慢（查询要扫更多数据）、备份文件也越来越大。

    但"删数据"是件危险的事，所以这个模块有三条硬规矩：

    ① **默认只预览、不执行**。CLI 不加 --apply 就只告诉你要删多少条、最早到哪天，
       一条都不会动。要真删必须显式说。这条是刻意的：
       "本想看看结果手一抖就删了" 是这类工具最经典的翻车方式。

    ② **先导出、再删除**。真要删的时候，默认先把要删的内容导成文件存起来
       （一个文件一天，JSONL 格式，记事本/Excel 都能看），确认文件写成功了才删。
       这样万一发现删错了（比如业务上其实还要查三年前的记录），文件还在。

    ③ **每一类数据单独一条规矩，绝不"一刀切"**。特别地：
       · 用量统计（usage_daily）是**计费依据**，永远不会被这里删掉 ——
         客户半年后说"上个月账单不对"，你得能查得出来。
       · 消息/日志只保留 N 天（可配置）。
       · 过期验证码、失效令牌是纯垃圾，过了宽限期就清。

    删除一律**分批**进行（每批 500 条），避免一次删几十万行把数据库锁住 ——
    那会让正在使用的用户卡住，比不清理还糟。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
from dataclasses import dataclass, field

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.login_failure import LoginFailure
from app.models.message import Message
from app.models.query_log import QueryLog
from app.models.refresh_token import RefreshToken
from app.models.verification_code import VerificationCode

logger = logging.getLogger("kbqa.maintenance")

# 每批删多少条。为什么分批：一次删几十万行会让数据库长时间持锁，
# 正在用的用户会被卡住 —— 那比不清理更糟。
BATCH_SIZE = 500

# 过期验证码/令牌的宽限期：过期后先留几天再清，
# 万一有人来查"我刚才那条验证码为什么失效"，还能查得到。
GRACE_DAYS = 7


@dataclass
class CleanupPlan:
    """一次清理的"计划"（先算清楚要删什么，再决定动不动手）。"""

    name: str
    description: str
    cutoff: dt.datetime | None
    count: int = 0
    note: str = ""

    def as_row(self) -> dict:
        return {
            "类别": self.name,
            "条数": self.count,
            "删除早于": self.cutoff.strftime("%Y-%m-%d %H:%M") if self.cutoff else "—",
            "说明": self.description,
        }


@dataclass
class CleanupReport:
    """一次清理的结果（预览和执行都返回这个结构）。"""

    applied: bool = False
    plans: list[CleanupPlan] = field(default_factory=list)
    deleted: dict[str, int] = field(default_factory=dict)
    archived_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_planned(self) -> int:
        return sum(p.count for p in self.plans)

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """把数据库取回的时间补齐时区（SQLite 存的是不带时区的，直接比会报错）。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


async def _count(db: AsyncSession, stmt) -> int:
    """数一数符合条件的行数。

    用 `select(func.count()).select_from(stmt.subquery())` 而不是 len(all())：
    后者会把几十万行数据全读进内存再数，清理脚本自己先把内存吃爆就尴尬了。
    """
    return int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


# ============================================================
# 各类数据的清理条件（单独成函数，便于单测逐条钉住）
# ============================================================


# 保留期的兜底值。⚠️ 特别注意"0 天"这件事：
#   如果配置里把保留期填成 0 或负数，**不能**理解成"连今天的都删"——
#   那几乎不可能是本意，而是配置写错了（比如占位符没改）。
#   此时一律回退成默认的 180 天，也就是"宁可多留、不要误删"。
#   这个取舍是刻意的：多留数据只是占点磁盘，误删数据是找不回来的。
#
#   写这段注释是因为本项目在别处已经踩过两次同类坑：
#   `if limit:` 会把 0 当成"不限"、`x or 默认值` 会把 0 换成默认值 ——
#   0 在这个项目里永远需要单独想一下它到底是什么意思。
DEFAULT_RETENTION_DAYS = 180


def _retention_days(raw, label: str, default: int = DEFAULT_RETENTION_DAYS) -> int:
    """把保留期配置值规整成一个可用的天数。

    ⚠️ 签名刻意收成"传值 + 传配置名"，而不是"传配置项名字、函数自己去读配置"。
    原因：有个自检会扫描"哪些配置项从来没被读过"（防"改了配置不生效还不报错"）。
    如果这里只把配置名当字符串传进去，扫描就看不到"这个配置真的被用了"，
    会把它误判成死配置 —— 而清死配置的下一步就是删掉它。
    显式传值让引用一目了然，扫描也能准确认出来。
    """
    try:
        days = int(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r 不是有效天数，已按默认 %d 天处理", label, raw, default)
        return default
    if days < 1:
        logger.warning(
            "%s=%s 不合法（必须至少 1 天）：填 0 会被理解成「一条都不留」，"
            "这几乎一定是配置写错了。已按默认 %d 天处理（宁可多留，不要误删）",
            label, days, default,
        )
        return default
    return days


def query_log_cutoff(now: dt.datetime | None = None) -> dt.datetime:
    """问答日志保留多久。"""
    days = _retention_days(settings.log_retention_days, "LOG_RETENTION_DAYS")
    return (now or _utcnow()) - dt.timedelta(days=days)


def message_cutoff(now: dt.datetime | None = None) -> dt.datetime:
    """聊天消息保留多久。"""
    days = _retention_days(settings.message_retention_days, "MESSAGE_RETENTION_DAYS")
    return (now or _utcnow()) - dt.timedelta(days=days)


async def plan_cleanup(db: AsyncSession) -> list[CleanupPlan]:
    """算出"如果要清理，会删掉什么"。**不改动任何数据**。"""
    now = _utcnow()
    plans: list[CleanupPlan] = []

    # 1) 问答日志
    ql_cut = query_log_cutoff(now)
    ql_stmt = select(QueryLog.id).where(QueryLog.created_at < ql_cut)
    plans.append(CleanupPlan(
        name="问答日志",
        description=f"保留最近 {_retention_days(settings.log_retention_days, 'LOG_RETENTION_DAYS')} 天",
        cutoff=ql_cut,
        count=await _count(db, ql_stmt),
    ))

    # 2) 聊天消息
    msg_cut = message_cutoff(now)
    msg_stmt = select(Message.id).where(Message.created_at < msg_cut)
    plans.append(CleanupPlan(
        name="聊天消息",
        description=f"保留最近 {_retention_days(settings.message_retention_days, 'MESSAGE_RETENTION_DAYS')} 天",
        cutoff=msg_cut,
        count=await _count(db, msg_stmt),
    ))

    # 3) 过期验证码（纯垃圾，过了宽限期就清）
    vc_cut = now - dt.timedelta(days=GRACE_DAYS)
    vc_stmt = select(VerificationCode.id).where(VerificationCode.expires_at < vc_cut)
    plans.append(CleanupPlan(
        name="过期验证码",
        description=f"过期且超过 {GRACE_DAYS} 天（纯垃圾数据）",
        cutoff=vc_cut,
        count=await _count(db, vc_stmt),
    ))

    # 4) 失效/过期的登录令牌
    rt_stmt = select(RefreshToken.id).where(
        or_(
            RefreshToken.revoked.is_(True),
            RefreshToken.expires_at < vc_cut,
        )
    )
    plans.append(CleanupPlan(
        name="失效登录令牌",
        description=f"已作废或过期超过 {GRACE_DAYS} 天（纯垃圾数据）",
        cutoff=vc_cut,
        count=await _count(db, rt_stmt),
    ))

    # 5) 登录失败记录（只是用来数"输错几次"，留太久没意义）
    lf_cut = now - dt.timedelta(days=max(30, GRACE_DAYS))
    lf_stmt = select(LoginFailure.id).where(LoginFailure.created_at < lf_cut)
    plans.append(CleanupPlan(
        name="登录失败记录",
        description="只用于短期风控计数，保留 30 天足够",
        cutoff=lf_cut,
        count=await _count(db, lf_stmt),
    ))

    # 6) 用量统计：**不清理**，只报告 —— 它是计费依据
    plans.append(CleanupPlan(
        name="用量统计（保留）",
        description="计费依据，永不自动删除",
        cutoff=None,
        count=0,
        note="客户半年后要核对账单时必须有据可查，所以这里只报告、不删",
    ))

    return plans


# ============================================================
# 归档（先导出成文件，再删）
# ============================================================


async def archive_rows(db: AsyncSession, stmt, out_path: pathlib.Path, columns: list[str]) -> int:
    """把符合条件的行导成 JSONL 文件（一行一条记录，记事本/Excel 都能看）。

    返回写出的条数。文件路径的上级目录会自动创建。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ 必须用 scalars() 而不是 execute()：
    #   execute(select(Model)) 返回的是"行包装对象"，直接 getattr(row, "question")
    #   会**静默拿到 None**（有默认值兜着，不报错）→ 归档文件里字段全是 null，
    #   等于归档了一堆废纸，而且不报错、看起来还"成功写了 3 条"。
    #   本项目真实踩过（靠"归档内容必须能读回问题原文"这条断言抓出来）。
    rows = (await db.scalars(stmt)).all()
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            record = {}
            for col in columns:
                value = getattr(row, col, None)
                # 时间要转成字符串，不然 json 会报"无法序列化"
                record[col] = value.isoformat() if isinstance(value, dt.datetime) else value
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(rows)


def _archive_path(kind: str, cutoff: dt.datetime) -> pathlib.Path:
    """归档文件名：带上类别与日期，便于按时间找。"""
    root = pathlib.Path(getattr(settings, "archive_dir", "data/archive") or "data/archive")
    stamp = _utcnow().strftime("%Y%m%d-%H%M%S")
    return root / f"{kind}-{cutoff.strftime('%Y%m%d')}-{stamp}.jsonl"


# ============================================================
# 执行清理
# ============================================================


async def _delete_in_batches(db: AsyncSession, model, condition) -> int:
    """分批删除，返回实际删除条数。

    为什么分批：一次删几十万行会让数据库长时间持锁，正在使用的用户会被卡住。
    做法是"查一批 id → 删这些 id → 提交"，循环到没有为止。
    """
    total = 0
    while True:
        ids = (
            await db.execute(select(model.id).where(condition).limit(BATCH_SIZE))
        ).scalars().all()
        if not ids:
            break
        await db.execute(delete(model).where(model.id.in_(list(ids))))
        await db.commit()
        total += len(ids)
    return total


async def run_cleanup(
    db: AsyncSession,
    *,
    apply: bool = False,
    archive: bool = True,
) -> CleanupReport:
    """执行（或只预览）一次清理。

    Args:
        db: 数据库会话。
        apply: **False = 只预览，不删任何东西**（默认）。True 才真的删。
        archive: 真删之前是否先把要删的内容导出成文件。**建议保持 True**。

    Returns:
        CleanupReport：计划删了什么、实际删了多少、归档文件在哪。
    """
    report = CleanupReport(applied=apply)
    report.plans = await plan_cleanup(db)

    if not apply:
        logger.info("清理预览完成：共 %d 条符合清理条件（未删除任何数据）", report.total_planned)
        return report

    now = _utcnow()

    async def _handle(name: str, model, condition, cutoff: dt.datetime, columns: list[str]):
        """统一的处理流程：可选归档 → 分批删除 → 记录结果。"""
        try:
            if archive:
                path = _archive_path(name, cutoff)
                written = await archive_rows(db, select(model).where(condition), path, columns)
                if written:
                    report.archived_files.append(str(path))
            report.deleted[name] = await _delete_in_batches(db, model, condition)
        except Exception as exc:
            # 某一类失败不影响其他类别；把错误收集起来最后一起报
            logger.exception("清理「%s」时出错", name)
            report.errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:150]}")

    ql_cut = query_log_cutoff(now)
    await _handle("问答日志", QueryLog, QueryLog.created_at < ql_cut, ql_cut,
                  # 归档要记下"恢复这条记录所需的全部字段"。
                  # 少了 question 就不知道问的什么，少了 retrieval 就查不出"当时为什么没答好"。
                  ["id", "user_id", "session_id", "question", "kb_scope",
                   "retrieval", "answered", "latency_ms", "created_at"])

    msg_cut = message_cutoff(now)
    await _handle("聊天消息", Message, Message.created_at < msg_cut, msg_cut,
                  ["id", "session_id", "role", "content", "feedback", "created_at"])

    vc_cut = now - dt.timedelta(days=GRACE_DAYS)
    await _handle("过期验证码", VerificationCode, VerificationCode.expires_at < vc_cut,
                  vc_cut, ["id", "kind", "target", "expires_at", "used_at", "created_at"])

    rt_cond = or_(RefreshToken.revoked.is_(True), RefreshToken.expires_at < vc_cut)
    await _handle("失效登录令牌", RefreshToken, rt_cond, vc_cut,
                  ["id", "user_id", "revoked", "expires_at", "created_at"])

    lf_cut = now - dt.timedelta(days=max(30, GRACE_DAYS))
    await _handle("登录失败记录", LoginFailure, LoginFailure.created_at < lf_cut, lf_cut,
                  ["id", "username", "ip", "created_at"])

    logger.info(
        "清理完成：实际删除 %d 条；归档文件 %d 个；错误 %d 个",
        report.total_deleted, len(report.archived_files), len(report.errors),
    )
    return report
