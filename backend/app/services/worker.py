"""解析队列工作进程（的"干活那部分"）：轮询任务表 → 领一个 → 跑完 → 再领。

工作原理（大白话）：
    它就是不断重复一句话："有没有到点的活？有就干，没有就歇一会儿再看。"
    之所以要"歇一会儿"（轮询间隔）而不是死循环空转，
    是因为空转会白白占满一个 CPU 核心、还会把数据库查询压力拉满。

启动时会先做两件事：
    ① **把僵尸任务放回队列** —— 上次进程被杀时领走但没做完的任务，
       否则它们会永远卡在"处理中"；
    ② 记一条日志告诉运维队列里现在积压了多少。

⚠️ 一个刻意的设计：**任何一条任务出错都不能让这个循环退出**。
    循环一旦退出，所有文档就再也不会被解析了，而且没有任何提示。
    所以每轮都用 try/except 包住，出错只记日志、继续下一轮。
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.services import job_queue

logger = logging.getLogger("kbqa.worker")

# 正在运行的工作循环（进程内单例，避免重复启动）
_worker_task: asyncio.Task | None = None


async def run_worker_once(worker: str) -> bool:
    """跑一轮：有活干返回 True，没有返回 False。"""
    async with AsyncSessionLocal() as db:
        return await job_queue.run_once(db, worker)


async def worker_loop(worker: str, poll_seconds: float | None = None) -> None:
    """工作循环：一直跑，直到被取消（服务关闭时）。"""
    interval = float(poll_seconds if poll_seconds is not None
                     else getattr(settings, "ingestion_poll_seconds", 2.0) or 2.0)

    # 启动恢复：把上次"领了活但没做完"的任务放回队列
    try:
        async with AsyncSessionLocal() as db:
            recovered = await job_queue.recover_stuck(db)
            st = await job_queue.stats(db)
        logger.info(
            "解析队列工作进程已启动（%s）：恢复僵尸任务 %d 条，当前队列 %s",
            worker, recovered, st,
        )
    except Exception:
        logger.exception("启动恢复失败（不影响后续轮询）")

    while True:
        try:
            worked = await run_worker_once(worker)
            if not worked:
                # 没活干 → 睡一会儿再看。用 sleep 而不是"立刻重查"，
                # 是为了不给数据库制造无意义的查询洪峰。
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("解析队列工作进程收到停止信号，正在退出")
            raise
        except Exception:
            # ⚠️ 这里**必须**吞掉异常：循环一旦退出，之后所有文档都不会被解析，
            # 而且不会有任何提示 —— 这种"静默停摆"是最难查的故障。
            logger.exception("解析任务执行出错（已跳过，继续处理下一条）")
            await asyncio.sleep(interval)


def start_worker() -> asyncio.Task | None:
    """在应用启动时调用：拉起工作循环（已在跑就不重复拉）。"""
    global _worker_task
    if not getattr(settings, "ingestion_worker_enabled", True):
        logger.info("解析队列工作进程已在配置中关闭（INGESTION_WORKER_ENABLED=false）")
        return None
    if _worker_task is not None and not _worker_task.done():
        return _worker_task
    _worker_task = asyncio.create_task(worker_loop(job_queue.worker_id()))
    return _worker_task


async def stop_worker() -> None:
    """在应用关闭时调用：停掉工作循环。"""
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except (asyncio.CancelledError, Exception):
        pass
    _worker_task = None
