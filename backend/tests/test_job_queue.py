"""解析任务队列测试。

这个文件在测什么（大白话）：
    把"解析文档"从"内存里起个后台任务"改成"写进数据库任务表，谁有空谁领"，
    是为了解决三个真实会发生的故障。所以这里逐条钉住这三个：

    ① **进程重启后任务不丢**：上次领了但没做完的任务，重启时要能自动接着做。
       不修的话，用户看到文档永远卡在"解析中"，只能删了重传。

    ② **多进程不会重复解析同一个文档**：两个进程同时抢，只能有一个领到。
       重复解析的后果是白花几倍向量化费用，而且两个进程同时写同一个知识库可能互相踩。

    ③ **失败会自动重试，且越试越慢**：解析失败最常见的原因是"调向量服务时网络抖了一下"，
       立刻重试往往还是失败。重试次数用完要停下来（否则坏文件会被无限重试、白烧钱）。

    另外还有两条"防蠢"的：重复入队要挡住、队列出错不能让工作循环退出。

运行方式（backend 目录下）：
    python -m unittest tests.test_job_queue -v
"""

import asyncio
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.models.document import Document
from app.models.ingestion_job import DONE, FAILED, PENDING, RUNNING, IngestionJob
from app.models.knowledge_base import KnowledgeBase
from app.models.organization import Organization
from app.services import job_queue as jq


def _run(coro):
    return asyncio.run(coro)


class Clock:
    """可控时钟（测试里不用真的等 5 秒、10 秒）。"""

    def __init__(self):
        self.t = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)

    def now(self) -> dt.datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = self.t + dt.timedelta(seconds=seconds)


class JobQueueTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        async def _seed():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self.Session() as db:
                db.add(Organization(id=1, name="甲公司", invite_code="c1"))
                db.add(KnowledgeBase(id=1, name="手册", org_id=1))
                await db.flush()
                # 造 3 个文档（任务挂在它们身上）
                for i in (1, 2, 3):
                    db.add(Document(id=i, kb_id=1, filename=f"资料{i}.txt",
                                    file_hash=f"hash-{i}", doc_type="txt", status="pending"))
                await db.commit()

        _run(_seed())

    def tearDown(self):
        _run(self.engine.dispose())

    async def _jobs(self) -> list[IngestionJob]:
        async with self.Session() as db:
            return list((await db.scalars(select(IngestionJob).order_by(IngestionJob.id))).all())

    # ================================================== 入队

    def test_enqueue_creates_pending_job(self):
        async def t():
            async with self.Session() as db:
                job = await jq.enqueue(db, doc_id=1, kb_id=1)
            self.assertEqual(job.status, PENDING)
            self.assertEqual(job.attempts, 0)
            self.assertIsNotNone(job.next_attempt_at)
        _run(t())

    def test_enqueue_is_idempotent(self):
        """**关键**：同一个文档重复入队不能排出多个任务。

        挡的是"用户手抖点了两次上传""批量上传脚本重跑了"。
        重复排队的后果是同一份文件被解析多遍 —— 白花几倍的向量化费用，
        而且两个任务同时写同一个知识库可能互相踩。
        """
        async def t():
            async with self.Session() as db:
                j1 = await jq.enqueue(db, doc_id=1, kb_id=1)
            async with self.Session() as db:
                j2 = await jq.enqueue(db, doc_id=1, kb_id=1)
            self.assertEqual(j1.id, j2.id, "重复入队生成了两条任务")
            jobs = await self._jobs()
            self.assertEqual(len(jobs), 1)
        _run(t())

    def test_enqueue_after_failed_creates_new_job(self):
        """彻底失败之后，用户点"重试"应该能真正排出一条新任务。

        为什么单钉：如果幂等判断把 failed 的任务也当成"还活着"，
        用户点重试就会毫无反应（没有报错，但也没重新解析），
        这种"点了没用"最难排查。
        """
        async def t():
            async with self.Session() as db:
                job = await jq.enqueue(db, doc_id=1, kb_id=1, max_attempts=1)
            async with self.Session() as db:
                found = await db.get(IngestionJob, job.id)
                await jq.claim_next(db, "w")
                await jq.mark_failed(db, found, "文件损坏", now=CLOCK.now())

            async with self.Session() as db:
                again = await jq.enqueue(db, doc_id=1, kb_id=1)
            self.assertNotEqual(again.id, job.id, "重试没有生成新任务")
            self.assertEqual(again.status, PENDING)
        CLOCK = Clock()
        _run(t())

    def test_enqueue_propagates_max_attempts(self):
        async def t():
            async with self.Session() as db:
                job = await jq.enqueue(db, doc_id=1, kb_id=1, max_attempts=7)
            self.assertEqual(job.max_attempts, 7)
        _run(t())

    # ================================================== 领取（原子性）

    def test_claim_marks_running_and_counts_attempt(self):
        async def t():
            async with self.Session() as db:
                await jq.enqueue(db, doc_id=1, kb_id=1)
            async with self.Session() as db:
                job = await jq.claim_next(db, "worker-A")
            self.assertIsNotNone(job)
            self.assertEqual(job.status, RUNNING)
            self.assertEqual(job.attempts, 1, "领取时就要把尝试次数加上，否则失败重试会算错")
            self.assertEqual(job.locked_by, "worker-A")
            self.assertIsNotNone(job.locked_at)
        _run(t())

    def test_two_workers_never_get_the_same_job(self):
        """**关键**：两次领取拿到的一定是**不同**的任务。

        多进程部署时两个进程同时来抢，绝不能都拿到同一个文档 ——
        那会重复解析、互相踩。这里通过"领走就改成 running"实现排他。
        """
        async def t():
            async with self.Session() as db:
                await jq.enqueue(db, doc_id=1, kb_id=1)
            async with self.Session() as db:
                await jq.enqueue(db, doc_id=2, kb_id=1)

            async with self.Session() as db:
                first = await jq.claim_next(db, "worker-A")
            async with self.Session() as db:
                second = await jq.claim_next(db, "worker-B")

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first.doc_id, second.doc_id,
                                "两个工作进程领到了同一个文档，会重复解析")
        _run(t())

    def test_claim_returns_none_when_nothing_due(self):
        """没有到点的任务时返回 None（工作进程据此决定"歇一会儿"）。"""
        async def t():
            async with self.Session() as db:
                self.assertIsNone(await jq.claim_next(db, "w"))
        _run(t())

    def test_not_due_job_is_not_claimed(self):
        """还没到重试时间点的任务不能被领走（退避要真的起作用）。"""
        async def t():
            clock = Clock()
            async with self.Session() as db:
                job = await jq.enqueue(db, doc_id=1, kb_id=1, now=clock.now())
            async with self.Session() as db:
                claimed = await jq.claim_next(db, "w", now=clock.now())
                await jq.mark_failed(db, claimed, "网络抖动", now=clock.now())

            # 失败后任务被推后，此刻不该被领走
            clock.advance(1)
            async with self.Session() as db:
                self.assertIsNone(await jq.claim_next(db, "w", now=clock.now()),
                                  "退避时间还没到就把任务领走了")

            # 等够了就能领
            clock.advance(jq.compute_backoff(1) + 1)
            async with self.Session() as db:
                self.assertIsNotNone(await jq.claim_next(db, "w", now=clock.now()))
        _run(t())

    # ================================================== 失败与重试

    def test_backoff_grows(self):
        """退避要一次比一次久（外部服务抽风时，越密集重试它越难恢复）。"""
        self.assertEqual(jq.compute_backoff(1), 5)
        self.assertEqual(jq.compute_backoff(2), 10)
        self.assertEqual(jq.compute_backoff(3), 20)
        # 封顶，别涨到几小时
        self.assertLessEqual(jq.compute_backoff(20), jq.MAX_BACKOFF_SECONDS)

    def test_failure_requeues_then_gives_up(self):
        """失败会重试，重试次数用完就彻底放弃（防坏文件被无限重试白烧钱）。"""
        async def t():
            clock = Clock()
            async with self.Session() as db:
                job = await jq.enqueue(db, doc_id=1, kb_id=1, max_attempts=2, now=clock.now())

            # 第 1 次失败 → 还会重试
            async with self.Session() as db:
                claimed = await jq.claim_next(db, "w", now=clock.now())
                will_retry = await jq.mark_failed(db, claimed, "第一次失败", now=clock.now())
            self.assertTrue(will_retry)
            async with self.Session() as db:
                cur = await db.get(IngestionJob, job.id)
                self.assertEqual(cur.status, PENDING)
                self.assertIn("第一次失败", cur.last_error)
                self.assertIsNone(cur.locked_by, "重试时应该把锁释放掉，否则别人领不走")

            # 第 2 次失败 → 到上限，彻底放弃
            clock.advance(jq.compute_backoff(1) + 1)
            async with self.Session() as db:
                claimed = await jq.claim_next(db, "w", now=clock.now())
                will_retry2 = await jq.mark_failed(db, claimed, "第二次失败", now=clock.now())
            self.assertFalse(will_retry2)
            async with self.Session() as db:
                cur = await db.get(IngestionJob, job.id)
                self.assertEqual(cur.status, FAILED)
                self.assertEqual(cur.attempts, 2)
        _run(t())

    def test_done_clears_error_and_lock(self):
        async def t():
            async with self.Session() as db:
                await jq.enqueue(db, doc_id=1, kb_id=1)
            async with self.Session() as db:
                job = await jq.claim_next(db, "w")
                await jq.mark_done(db, job)
            async with self.Session() as db:
                cur = await db.get(IngestionJob, job.id)
                self.assertEqual(cur.status, DONE)
                self.assertIsNone(cur.locked_by)
                self.assertIsNone(cur.locked_at, "成功后要把锁清掉，否则会被当成僵尸任务")
        _run(t())

    # ================================================== 重启恢复

    def test_recover_stuck_requeues_zombies(self):
        """**关键**：进程被杀留下的"处理中"任务，重启要能自动接着做。

        不修的话用户会看到文档永远卡在"解析中"，
        既等不到结果、也不能重新解析（有活跃任务挡着），最后只能删了重传。
        """
        async def t():
            clock = Clock()
            async with self.Session() as db:
                await jq.enqueue(db, doc_id=1, kb_id=1, now=clock.now())
            async with self.Session() as db:
                await jq.claim_next(db, "worker-that-died", now=clock.now())

            # 刚过 10 秒：还不算僵尸（进程可能只是正在慢慢解析）
            clock.advance(10)
            async with self.Session() as db:
                n = await jq.recover_stuck(db, stale_seconds=900, now=clock.now())
            self.assertEqual(n, 0, "刚领走不久就判定成僵尸，会把正在做的活抢走")

            # 过了 16 分钟：认定领它的进程已经死了
            clock.advance(1000)
            async with self.Session() as db:
                n = await jq.recover_stuck(db, stale_seconds=900, now=clock.now())
            self.assertEqual(n, 1)
            async with self.Session() as db:
                job = (await db.scalars(select(IngestionJob))).one()
                self.assertEqual(job.status, PENDING)
                self.assertIsNone(job.locked_by)

            # 恢复之后必须能真的被重新领走
            async with self.Session() as db:
                again = await jq.claim_next(db, "worker-new", now=clock.now())
            self.assertIsNotNone(again, "僵尸任务放回队列后却领不走")
        _run(t())

    def test_recover_ignores_pending_and_done(self):
        """恢复只该动"处理中"的，不能把已完成/排队中的搅乱。"""
        async def t():
            clock = Clock()
            async with self.Session() as db:
                await jq.enqueue(db, doc_id=1, kb_id=1, now=clock.now())
                await jq.enqueue(db, doc_id=2, kb_id=1, now=clock.now())
            async with self.Session() as db:
                job = await jq.claim_next(db, "w", now=clock.now())
                await jq.mark_done(db, job)

            clock.advance(10000)
            async with self.Session() as db:
                n = await jq.recover_stuck(db, stale_seconds=900, now=clock.now())
            self.assertEqual(n, 0, "把已完成的任务也当成僵尸了")

            async with self.Session() as db:
                statuses = sorted(
                    j.status for j in (await db.scalars(select(IngestionJob))).all()
                )
            self.assertEqual(statuses, [DONE, PENDING])
        _run(t())

    # ================================================== 统计

    def test_stats_shape(self):
        """队列概况（运维要靠它看"积压了多少、失败了多少"）。"""
        async def t():
            async with self.Session() as db:
                await jq.enqueue(db, doc_id=1, kb_id=1)
                await jq.enqueue(db, doc_id=2, kb_id=1)
            async with self.Session() as db:
                job = await jq.claim_next(db, "w")
                await jq.mark_done(db, job)

            async with self.Session() as db:
                st = await jq.stats(db)
            self.assertEqual(st["done"], 1)
            self.assertEqual(st["pending"], 1)
            self.assertEqual(st["running"], 0)
            self.assertEqual(st["failed"], 0)
            for key in ("pending", "running", "done", "failed"):
                self.assertIn(key, st)
        _run(t())

    def test_stats_is_a_snapshot_not_hardcoded(self):
        """统计数字必须来自数据库（否则后台显示永远是 0，看着"很闲"其实一堆积压）。"""
        async def t():
            async with self.Session() as db:
                await jq.enqueue(db, doc_id=1, kb_id=1)
                await jq.enqueue(db, doc_id=2, kb_id=1)
                await jq.enqueue(db, doc_id=3, kb_id=1)
                st = await jq.stats(db)
            self.assertEqual(st["pending"], 3)
        _run(t())

    # ================================================== 工作进程

    def test_worker_once_reports_whether_it_worked(self):
        """工作进程要有明确的"这次干到活没"信号，才能决定睡多久。"""
        async def t():
            from app.services import worker as wk

            async with self.Session() as db:
                # 没有任务 → 没干到活
                self.assertFalse(await jq.run_once(db, "w"))

            async with self.Session() as db:
                await jq.enqueue(db, doc_id=1, kb_id=1)

            # 领到了任务就去执行 process_document；这里把执行换成假的，
            # 避免测试真的去解析文件、调向量服务
            called = {"doc_id": None}

            async def fake_process(doc_id):
                called["doc_id"] = doc_id

            from app.services import ingestion as ing
            orig = ing.process_document
            ing.process_document = fake_process
            try:
                async with self.Session() as db:
                    worked = await jq.run_once(db, "w")
            finally:
                ing.process_document = orig

            self.assertTrue(worked)
            self.assertEqual(called["doc_id"], 1, "领到任务却执行了别的文档？")

            async with self.Session() as db:
                job = (await db.scalars(select(IngestionJob))).one()
            self.assertEqual(job.status, DONE)
            _ = wk
        _run(t())

    def test_worker_marks_failed_when_processing_raises(self):
        """解析抛异常时要落到"失败并重试"，不能静默当成成功。"""
        async def t():
            from app.services import ingestion as ing

            async with self.Session() as db:
                await jq.enqueue(db, doc_id=1, kb_id=1)

            async def boom(doc_id):
                raise RuntimeError("解析炸了")

            orig = ing.process_document
            ing.process_document = boom
            try:
                async with self.Session() as db:
                    worked = await jq.run_once(db, "w")
            finally:
                ing.process_document = orig

            self.assertTrue(worked)
            async with self.Session() as db:
                job = (await db.scalars(select(IngestionJob))).one()
            self.assertEqual(job.status, PENDING, "失败后应该回到队列准备重试")
            self.assertIn("解析炸了", job.last_error)
        _run(t())

    def test_worker_id_is_unique_per_process(self):
        """工作进程的名字要互不相同（出问题时才能看出是哪个进程卡住了）。"""
        ids = {jq.worker_id() for _ in range(20)}
        self.assertEqual(len(ids), 20)


if __name__ == "__main__":
    unittest.main()
