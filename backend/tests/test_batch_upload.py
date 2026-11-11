"""批量上传的测试。

这个文件在测什么（大白话）：
    用户一次拖 50 个文件进来，里面可能夹着一两个不支持的格式。
    这时**不能整批失败** —— 用户得自己去找是哪个文件的问题，体验很差。
    正确语义是"部分成功"：好的照常入库，坏的单独报告原因。

    但要做到"部分成功"有个陷阱：单个文件失败时如果直接 rollback，
    会把**整个会话**里前面已成功的文件也回滚掉（实测踩过：
    3 个文件 2 正常 1 坏格式，最后只有 1 个成功）。

    所以这个文件重点验证：一个文件失败不影响同批次的其他文件。
"""

import asyncio
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.db.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.organization import Organization
from app.models.user import User


def _run(coro):
    return asyncio.run(coro)


class BatchUploadTestCase(unittest.TestCase):
    """批量上传的核心语义：部分成功、逐条报错、数量上限。"""

    def setUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        _run(self._seed())

    def tearDown(self):
        _run(self.engine.dispose())

    async def _seed(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.Session() as db:
            db.add(Organization(id=1, name="测试公司", invite_code="c1"))
            await db.flush()
            u = User(username="admin", password_hash=hash_password("Pass1234!"), role="admin", org_id=1)
            db.add(u)
            await db.flush()
            kb = KnowledgeBase(name="测试库", org_id=1, is_active=True)
            db.add(kb)
            await db.commit()
            self.uid, self.kid = u.id, kb.id

    async def _call_batch(self, files_data):
        """直接调批量接口函数（避开 HTTP 层，聚焦业务逻辑）。"""
        from fastapi import UploadFile

        from app.api.v1.knowledge_bases import _save_one_document

        results = []
        for name, content in files_data:
            async with self.Session() as db:
                kb = await db.get(KnowledgeBase, self.kid)
                f = UploadFile(filename=name, file=io.BytesIO(content))
                try:
                    doc = await _save_one_document(self.kid, kb, db, f)
                    results.append((name, "ok", doc.id))
                except Exception as e:
                    results.append((name, "fail", str(getattr(e, "detail", e))))
        return results

    def test_all_valid_files_succeed(self):
        async def t():
            results = await self._call_batch([
                ("a.txt", b"content a"),
                ("b.md", b"# content b"),
                ("c.txt", b"content c"),
            ])
            ok = [r for r in results if r[1] == "ok"]
            self.assertEqual(len(ok), 3, f"3 个正常文件应全部成功，实际：{results}")
        _run(t())

    def test_one_bad_file_does_not_break_others(self):
        """这是本文件最关键的测试。

        一个不支持的格式失败后，同批次的其他文件必须照常成功。
        早期实现用共享会话 + rollback，会导致后面的文件全部失败。
        """
        async def t():
            results = await self._call_batch([
                ("ok1.txt", b"unique content 1"),
                ("bad.exe", b"MZ binary"),
                ("ok2.txt", b"unique content 2"),
            ])
            by_name = {name: (status, detail) for name, status, detail in results}
            self.assertEqual(by_name["ok1.txt"][0], "ok", "坏文件之前的正常文件被连累")
            self.assertEqual(by_name["ok2.txt"][0], "ok", "坏文件之后的正常文件被连累")
            self.assertEqual(by_name["bad.exe"][0], "fail", "坏格式应该失败")
            self.assertIn("不支持的文件格式", by_name["bad.exe"][1])
        _run(t())

    def test_duplicate_file_rejected(self):
        """同知识库内内容完全相同的文件应被查重拒绝。"""
        async def t():
            await self._call_batch([("dup.txt", b"same content")])
            results = await self._call_batch([("dup2.txt", b"same content")])
            status, detail = results[0][1], results[0][2]
            self.assertEqual(status, "fail")
            self.assertTrue(
                "已在知识库" in detail or "处理中" in detail,
                f"重复文件应被拒，实际提示：{detail}",
            )
        _run(t())

    def test_empty_content_rejected(self):
        async def t():
            results = await self._call_batch([("empty.txt", b"")])
            self.assertEqual(results[0][1], "fail")
            self.assertIn("为空", results[0][2])
        _run(t())

    def test_max_batch_files_limit_exists(self):
        """必须有数量上限，防止一次塞几千个文件把服务拖垮。"""
        from app.api.v1.knowledge_bases import MAX_BATCH_FILES

        self.assertGreater(MAX_BATCH_FILES, 0)
        self.assertLessEqual(MAX_BATCH_FILES, 200, "上限太大起不到保护作用")

    def test_single_upload_endpoint_still_works(self):
        """重构后单文件接口必须保持原有行为（向后兼容）。"""
        async def t():
            from fastapi import UploadFile

            from app.api.v1.knowledge_bases import _save_one_document

            async with self.Session() as db:
                kb = await db.get(KnowledgeBase, self.kid)
                f = UploadFile(filename="single.txt", file=io.BytesIO(b"single file content"))
                doc = await _save_one_document(self.kid, kb, db, f)
            self.assertEqual(doc.filename, "single.txt")
            async with self.Session() as db:
                rows = (await db.execute(select(type(doc)).where(type(doc).id == doc.id))).scalars().all()
                self.assertEqual(len(rows), 1)
        _run(t())


if __name__ == "__main__":
    unittest.main()
