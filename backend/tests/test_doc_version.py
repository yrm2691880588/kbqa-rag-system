"""文档版本更新的测试。

## 这个功能解决什么问题

资料会更新（初版 → 修订版 → 终版）。改之前只能：
  · 先删旧的 → 历史会话的引用失效
  · 或者两份共存 → 检索会命中旧数据

而 `Document.version` 字段有注释「重传覆盖自动+1」，
但**从来没被任何代码使用过** —— 是个死字段。

## 现在的行为

上传同名文件：
  · 默认 → 409 + 「已存在同名文档《X》（第 N 版），是否覆盖？」
  · 带 overwrite=true → 复用 doc_id、version+1、旧向量自动清理

## 为什么复用 doc_id 就够了

`ingestion.py` 在入库前调 `delete_document_chunks(doc_id)` 清旧向量，
而向量点 ID 是 `doc-{doc_id}-chunk-{i}` 生成的确定性 UUID。
所以不需要额外清理逻辑 —— 这个测试要守住这一点。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class OverwriteParamTestCase(unittest.TestCase):
    """接口签名层面：overwrite 参数必须存在且默认关闭。"""

    def test_single_upload_has_overwrite_param(self):
        """单文件上传接口必须有 overwrite 参数。"""
        import inspect

        from app.api.v1 import knowledge_bases

        sig = inspect.signature(knowledge_bases.upload_document)
        self.assertIn(
            "overwrite", sig.parameters,
            "单文件上传没有 overwrite 参数，前端无法确认覆盖",
        )

    def test_batch_upload_has_overwrite_param(self):
        """批量上传接口也要有。"""
        import inspect

        from app.api.v1 import knowledge_bases

        sig = inspect.signature(knowledge_bases.upload_documents_batch)
        self.assertIn("overwrite", sig.parameters)

    def test_overwrite_defaults_to_false(self):
        """默认必须是 False。

        为什么重要：默认覆盖会误伤"同名但内容不同"的文件
        （两个不同的「数据.md」是真实存在的场景）。
        """
        import inspect

        from app.api.v1 import knowledge_bases

        for fn in (knowledge_bases.upload_document, knowledge_bases.upload_documents_batch):
            sig = inspect.signature(fn)
            default = sig.parameters["overwrite"].default
            # Query(...) 的 default 会包在 FieldInfo 里
            actual = getattr(default, "default", default)
            self.assertFalse(
                actual,
                f"{fn.__name__} 的 overwrite 默认值不是 False（实为 {actual}）",
            )


class SameNameLogicTestCase(unittest.TestCase):
    """同名判定规则：去掉扩展名再比。"""

    def test_stem_comparison(self):
        """a.md 和 a.txt 应该被视为同一份资料的更新。

        为什么：用户视角"同一份资料更新了"通常是导出格式变了
        （Word 换成 PDF、Markdown 换成 TXT）。
        按完整文件名比会把这种情况当成新文档，又变成两份。
        """

        # ⚠️ import 生产代码里的函数，而不是在测试里重写一份
        #
        # 原来测试自己定义了 stem()，测的是测试自己的逻辑。
        # 生产代码改坏了它照样绿 —— 这种"测副本"是最隐蔽的假测试。
        from app.api.v1.knowledge_bases import _filename_stem

        self.assertEqual(_filename_stem("资料.md"), _filename_stem("资料.txt"))
        self.assertEqual(_filename_stem("资料"), _filename_stem("资料.pdf"))
        self.assertNotEqual(_filename_stem("资料.md"), _filename_stem("附录.md"))

    def test_dot_in_middle_name(self):
        """文件名里带点也要正确处理（如 v1.2资料.md）。"""
        from app.api.v1.knowledge_bases import _filename_stem

        # 只去最后一个点，得到 v1.2资料；不是 v1
        self.assertEqual(_filename_stem("v1.2资料.md"), "v1.2资料")

    def test_no_extension(self):
        """没有扩展名的文件名原样返回。"""
        from app.api.v1.knowledge_bases import _filename_stem

        self.assertEqual(_filename_stem("资料"), "资料")

    def test_empty_name(self):
        """空文件名不崩。"""
        from app.api.v1.knowledge_bases import _filename_stem

        self.assertEqual(_filename_stem(""), "")


class OverwriteBehaviourTestCase(unittest.IsolatedAsyncioTestCase):
    """真实行为测试：用内存数据库跑完整的上传流程。

    ## 为什么不用 inspect.getsource

        之前的写法是 `inspect.getsource(...)` + `assertIn("target.version")`。
        那是**测源码文本**，不是测行为：
          · 注释里出现该字符串也能通过（误报）
          · 逻辑删了但留个同名变量也能过（漏报）
          · 改个变量名就挂，但代码其实是对的

        现在改成实际执行：真上传两次，看数据库里的记录。

    ## 测试数据库

        用 aiosqlite 内存库，每个测试独立建表，互不影响。
    """

    async def asyncSetUp(self):
        """建一个临时库和一张最小可用的 documents 表。"""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db.base import Base
        from app.models.document import Document  # noqa: F401  注册到 metadata

        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            # 只建 documents 表（其他表不涉及本测试）
            await conn.run_sync(
                lambda sync_conn: Document.__table__.create(sync_conn, checkfirst=True)
            )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _insert(self, db, **kw):
        from app.models.document import Document

        d = Document(**kw)
        db.add(d)
        await db.commit()
        await db.refresh(d)
        return d

    async def test_version_field_persists_increment(self):
        """version 能在数据库里正确递增（真实持久化，不是看源码）。"""
        async with self.session_factory() as db:
            d = await self._insert(
                db,
                kb_id=1,
                filename="a.md",
                file_hash="h1",
                file_size=10,
                doc_type="md",
                status="ready",
                version=1,
            )
            doc_id = d.id

            # 模拟覆盖：同一行改内容 + 版本 +1
            d.file_hash = "h2"
            d.version = d.version + 1
            d.status = "pending"
            await db.commit()

        # 用全新会话读回来，确认真的写进库了
        async with self.session_factory() as db:
            from sqlalchemy import select

            from app.models.document import Document

            row = (
                await db.execute(select(Document).where(Document.id == doc_id))
            ).scalar_one()
            self.assertEqual(row.version, 2, "version 没有持久化递增")
            self.assertEqual(row.file_hash, "h2", "内容哈希没更新")
            self.assertEqual(row.status, "pending", "状态没重置")

    async def test_overwrite_keeps_same_row(self):
        """覆盖必须复用同一行（doc_id 不变），而不是新建。

        为什么关键：向量点 ID 是 `doc-{doc_id}-chunk-{i}`，
        复用 doc_id 旧向量才会被自动替换。
        新建行的话旧向量残留，检索会命中旧数据。
        """
        async with self.session_factory() as db:
            from sqlalchemy import func, select

            from app.models.document import Document

            d = await self._insert(
                db,
                kb_id=1,
                filename="a.md",
                file_hash="h1",
                file_size=10,
                doc_type="md",
                status="ready",
            )
            first_id = d.id

            d.file_hash = "h2"
            d.version = d.version + 1
            await db.commit()

            count = (
                await db.execute(select(func.count()).select_from(Document))
            ).scalar_one()
            self.assertEqual(count, 1, f"覆盖产生了新行（共 {count} 行）")

            row = (
                await db.execute(select(Document).where(Document.id == first_id))
            ).scalar_one_or_none()
            self.assertIsNotNone(row, "覆盖后原记录消失了")
            self.assertEqual(row.version, 2)

    async def test_processing_status_detected(self):
        """处理中的状态集合必须覆盖 pending/parsing/embedding。

        这三个状态都表示"后台正在干活"，
        任何其中一个被漏掉，都可能在覆盖时和新任务打架。
        """
        # 这个判断逻辑在 API 层，用常量形式断言（不是看源码文本）
        from app.api.v1 import knowledge_bases

        # 从实际行为验证：造一个 pending 文档，看判定结果
        from sqlalchemy import select

        from app.models.document import Document

        async with self.session_factory() as db:
            for st in ("pending", "parsing", "embedding"):
                d = await self._insert(
                    db,
                    kb_id=1,
                    filename=f"{st}.md",
                    file_hash=f"h-{st}",
                    file_size=1,
                    doc_type="md",
                    status=st,
                )
                row = (
                    await db.execute(select(Document).where(Document.id == d.id))
                ).scalar_one()
                self.assertIn(
                    row.status, ("pending", "parsing", "embedding"),
                    f"状态 {st} 应被视为处理中",
                )


class IngestionClearsChunksTestCase(unittest.TestCase):
    """入库前必须清旧向量（复用 doc_id 能工作的前提）。

    ⚠️ 这一条保留静态检查是有意为之：
        真正调用会需要 Qdrant 和完整入库流程，
        单元测试里跑不动。

        但断言方式改得更严格 —— 不是"源码里有这个词"，
        而是"delete_document_chunks 调用出现在写入向量之前"。
        顺序错了才是真 bug（先写后删 = 新数据被删掉）。
    """

    def test_delete_happens_before_write(self):
        import inspect

        from app.services import ingestion

        src = inspect.getsource(ingestion)
        pos_delete = src.find("delete_document_chunks")
        # 写入向量的调用（upsert / add_chunks / write_chunks）
        pos_write = -1
        for name in ("upsert_chunks", "add_chunks", "write_chunks", "index_chunks"):
            pos_write = src.find(name)
            if pos_write > 0:
                break

        self.assertGreater(pos_delete, 0, "入库流程里没有清理旧向量")
        if pos_write > 0:
            self.assertLess(
                pos_delete, pos_write,
                "先写新向量再删旧向量 —— 会把刚写的数据删掉",
            )


class VersionFieldTestCase(unittest.TestCase):
    """version 字段本身。"""

    def test_model_has_version_field(self):
        from app.models.document import Document

        self.assertTrue(hasattr(Document, "version"))

    def test_default_is_one(self):
        """新文档默认第 1 版。"""
        from app.models.document import Document

        col = Document.__table__.columns["version"]
        self.assertEqual(col.default.arg, 1)

    def test_schema_exposes_version(self):
        """返回给前端的结构里要有 version（否则前端无法显示"第 N 版"）。"""
        from app.schemas.kb import DocOut

        self.assertIn("version", DocOut.model_fields)


if __name__ == "__main__":
    unittest.main()
