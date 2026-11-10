"""解析任务队列（P2-15）：入队 → 原子领取 → 执行 → 成功/失败重试。

四个关键机制（每一个都对应一次真实会发生的故障）：

  ① **幂等入队**：同一个文档已经有"排队中/正在处理"的任务时，不再重复排。
     挡的是"用户重复点上传"和"批量上传脚本重跑" —— 重复排队的后果是
     同一份文件被解析多遍（白花几倍的向量化费用），而且并发写同一个知识库可能互相踩。

  ② **原子领取**：多进程同时抢任务时，用数据库的行锁（PostgreSQL 的
     `FOR UPDATE SKIP LOCKED`）保证**一个任务只会被一个进程领走**。
     没有这个，两个进程会同时解析同一个文档，最终的文档状态变成"谁后写谁赢"的随机结果。

  ③ **失败重试 + 指数退避**：失败后不是立刻重试，而是等 5 秒、10 秒、20 秒……
     因为最常见的失败原因是"调向量服务时网络抖了一下"，
     立刻重试往往还是失败，还平白多压一次外部服务。

  ④ **重启恢复**：进程被杀时，它领走但没做完的任务会永远停在"处理中"。
     启动时把"锁定时间已经过久"的任务放回队列（叫它僵尸任务），让它们能接着被处理。
     没有这一步，用户会看到文档永远卡在"解析中"，只能删了重传。
"""

from __future__ import annotations

import datetime as dt
import logging
import socket
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.ingestion_job import DONE, FAILED, PENDING, RUNNING, IngestionJob

logger = logging.getLogger("kbqa.job_queue")

# 失败后的基础等待时间（秒）。第 1 次失败等 5 秒、第 2 次等 10 秒、第 3 次等 20 秒……
BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 300


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def worker_id() -> str:
    """给当前进程起个名字（主机名 + 随机后缀）。

    用途：任务里记下"是谁领走的"，出问题时能看出"是哪个进程卡住了"。
    多进程部署时这个名字必须互不相同，所以带上随机后缀。
    """
    return f"{socket.gethostname()[:32]}-{uuid.uuid4().hex[:6]}"


def compute_backoff(attempts: int) -> int:
    """第 attempts 次失败后应等待几秒（2 的幂增长，封顶 5 分钟）。

    例：第 1 次失败等 5 秒、第 2 次 10 秒、第 3 次 20 秒……
    为什么要"越来越久"：外部服务如果正在抽风，我们越密集地重试，它越难恢复。
    """
    return int(min(BASE_BACKOFF_SECONDS * (2 ** max(0, attempts - 1)), MAX_BACKOFF_SECONDS))


async def find_active(db: AsyncSession, doc_id: int) -> IngestionJob | None:
    """找这个文档"还没做完"的任务（排队中或处理中）；没有则返回 None。

    用途：① 入队前的幂等判断；② 接口层判断"这个文档是不是已经在处理了"。
    以前这个判断靠的是一个内存里的字典（进程内），多进程部署时
    在 A 进程看不到 B 进程正在处理的文档 —— 于是同一个文档被排两遍。
    现在查数据库，多进程看到的答案是一致的。
    """
    return await db.scalar(
        select(IngestionJob)
        .where(
            IngestionJob.doc_id == doc_id,
            IngestionJob.status.in_([PENDING, RUNNING]),
        )
        .order_by(IngestionJob.id.desc())
        .limit(1)
    )


async def enqueue(
    db: AsyncSession,
    doc_id: int,
    kb_id: int,
    max_attempts: int | None = None,
    now: dt.datetime | None = None,
) -> IngestionJob:
    """把一个文档排进解析队列（幂等：已有活跃任务就直接返回它）。"""
    now = now or _utcnow()

    existing = await find_active(db, doc_id)
    if existing is not None:
        # 已经有任务在排队或处理中 → 不重复排。这正是"用户重复点上传"时的正确行为。
        return existing

    job = IngestionJob(
        doc_id=doc_id,
        kb_id=kb_id,
        status=PENDING,
        attempts=0,
        max_attempts=int(max_attempts or getattr(settings, "ingestion_max_attempts", 3) or 3),
        next_attempt_at=now,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    logger.info("已入队解析任务：文档 %s（任务 %s）", doc_id, job.id)
    return job


async def claim_next(
    db: AsyncSession, worker: str, now: dt.datetime | None = None
) -> IngestionJob | None:
    """领取一个"到点了"的任务并标记为处理中；没有就返回 None。

    多进程安全：PostgreSQL 下用 `FOR UPDATE SKIP LOCKED` ——
    意思是"锁住我能拿到的这一行，别人已经在处理的行直接跳过"。
    这样十个进程同时来抢，也不会有人拿到同一个任务。
    """
    now = now or _utcnow()

    stmt = (
        select(IngestionJob)
        .where(
            IngestionJob.status == PENDING,
            IngestionJob.next_attempt_at <= now,
        )
        .order_by(IngestionJob.next_attempt_at, IngestionJob.id)
        .limit(1)
    )
    if not settings.is_sqlite:
        # SQLite 是单写入者的，用不上行锁；硬套反而会报不支持
        stmt = stmt.with_for_update(skip_locked=True)

    job = await db.scalar(stmt)
    if job is None:
        return None

    job.status = RUNNING
    job.locked_at = now
    job.locked_by = worker
    job.attempts = (job.attempts or 0) + 1
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def mark_done(db: AsyncSession, job: IngestionJob) -> None:
    """标记任务成功。"""
    job.status = DONE
    job.last_error = None
    job.locked_at = None
    job.locked_by = None
    db.add(job)
    await db.commit()


async def mark_failed(
    db: AsyncSession, job: IngestionJob, error: str, now: dt.datetime | None = None
) -> bool:
    """标记任务失败：还能重试就放回队列，重试次数用完就彻底放弃。

    Returns:
        True = 还会重试；False = 已彻底失败（需要人工介入）。
    """
    now = now or _utcnow()
    job.last_error = str(error)[:2000]
    job.locked_at = None
    job.locked_by = None

    if (job.attempts or 0) < (job.max_attempts or 1):
        wait = compute_backoff(job.attempts or 1)
        job.status = PENDING
        job.next_attempt_at = now + dt.timedelta(seconds=wait)
        db.add(job)
        await db.commit()
        logger.warning(
            "解析任务 %s 第 %s 次失败，%s 秒后重试：%s",
            job.id, job.attempts, wait, job.last_error[:200],
        )
        return True

    job.status = FAILED
    db.add(job)
    await db.commit()
    logger.error(
        "解析任务 %s 已重试 %s 次仍失败，放弃：%s",
        job.id, job.attempts, job.last_error[:200],
    )
    return False


async def recover_stuck(
    db: AsyncSession, stale_seconds: int | None = None, now: dt.datetime | None = None
) -> int:
    """把"僵尸任务"放回队列，返回处理了几条。

    什么叫僵尸任务：状态是"处理中"，但领它的进程已经没了
    （被发版重启、被操作系统杀掉、容器崩了）。
    判断依据是"锁定时间已经超过某个阈值"——超过就认为领它的进程已经死了。

    为什么这件事必须在启动时做：
        不做的话，这些任务会永远停在"解析中"。用户看到文档卡住，
        既不能重新解析（有活跃任务，入队会被幂等挡住），
        也不知道要等多久，最后只能删了重传 —— 体验很差。
    """
    now = now or _utcnow()
    stale = int(stale_seconds if stale_seconds is not None
                else getattr(settings, "ingestion_stale_seconds", 900) or 900)
    cutoff = now - dt.timedelta(seconds=stale)

    # 注意这里用"批量 UPDATE"而不是"查出来逐条改"：
    # 僵尸任务多的时候，逐条改会产生大量 SQL 和一次很长的提交，启动会变慢。
    result = await db.execute(
        update(IngestionJob)
        .where(
            IngestionJob.status == RUNNING,
            IngestionJob.locked_at.is_not(None),
            IngestionJob.locked_at < cutoff,
        )
        .values(status=PENDING, locked_at=None, locked_by=None, next_attempt_at=now)
    )
    await db.commit()
    count = int(result.rowcount or 0)
    if count:
        logger.warning(
            "启动恢复：把 %d 条「处理中但没有进程在管」的任务放回了队列"
            "（多半是上次重启/被强杀时留下的）", count
        )
    return count


async def stats(db: AsyncSession) -> dict:
    """队列概况（给管理后台/运维看：积压了多少、失败了多少）。"""
    rows = (
        await db.execute(
            select(IngestionJob.status, func.count()).group_by(IngestionJob.status)
        )
    ).all()
    by_status = {str(s): int(c) for s, c in rows}
    return {
        "pending": by_status.get(PENDING, 0),
        "running": by_status.get(RUNNING, 0),
        "done": by_status.get(DONE, 0),
        "failed": by_status.get(FAILED, 0),
    }


async def run_once(db: AsyncSession, worker: str) -> bool:
    """领一个任务并执行；返回"这次有没有干到活"。

    从 app.services.ingestion 里**延迟导入** process_document：
    ingestion 模块依赖队列（入队），这里又依赖它（执行），
    写在文件顶部会形成循环导入，服务直接起不来。
    """
    from app.services.ingestion import process_document

    job = await claim_next(db, worker)
    if job is None:
        return False

    try:
        await process_document(job.doc_id)
    except Exception as exc:
        await mark_failed(db, job, f"{type(exc).__name__}: {exc}")
        return True

    await mark_done(db, job)
    return True
