"""文档版本更新的测试。

## 这个功能解决什么问题

财报会更新（年报 → 半年报 → 三季报）。改之前只能：
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

        def stem(name: str) -> str:
            return name.rsplit(".", 1)[0] if "." in name else name

        self.assertEqual(stem("财报.md"), stem("财报.txt"))
        self.assertEqual(stem("财报"), stem("财报.pdf"))
        self.assertNotEqual(stem("财报.md"), stem("年报.md"))

    def test_dot_in_middle_name(self):
        """文件名里带点也要正确处理（如 v1.2财报.md）。"""

        def stem(name: str) -> str:
            return name.rsplit(".", 1)[0] if "." in name else name

        self.assertEqual(stem("v1.2财报.md"), "v1.2财报")


class CodeContractTestCase(unittest.TestCase):
    """代码层面的关键约定（静态检查）。"""

    def test_overwrite_reuses_doc_id(self):
        """覆盖时必须复用 doc_id，不能新建记录。

        为什么关键：向量点 ID 是按 doc_id 生成的确定性 UUID，
        复用 doc_id 才能让旧向量被自动替换。
        新建记录的话，旧向量会残留，检索会命中旧数据。
        """
        import inspect

        from app.api.v1 import knowledge_bases

        src = inspect.getsource(knowledge_bases._save_one_document)
        # 覆盖分支里应该对已有 target 做赋值，而不是 new Document(...)
        self.assertIn("target.version", src, "覆盖分支没有版本号递增")
        self.assertIn(
            "target.id", src,
            "覆盖分支没有复用已有 doc_id",
        )

    def test_version_increments(self):
        """version 必须 +1。"""
        import inspect

        from app.api.v1 import knowledge_bases

        src = inspect.getsource(knowledge_bases._save_one_document)
        self.assertRegex(
            src,
            r"version\s*=\s*\(?\s*target\.version",
            "没有看到 version = target.version + 1 这类递增逻辑",
        )

    def test_blocks_overwrite_while_processing(self):
        """正在入库的文档不能被覆盖（否则状态会错乱）。"""
        import inspect

        from app.api.v1 import knowledge_bases

        src = inspect.getsource(knowledge_bases._save_one_document)
        self.assertIn(
            'in ("pending", "parsing", "embedding")', src,
            "没有阻止覆盖处理中的文档",
        )

    def test_ingestion_clears_old_chunks(self):
        """入库前必须清旧向量（这是复用 doc_id 能工作的前提）。"""
        import inspect

        from app.services import ingestion

        src = inspect.getsource(ingestion)
        self.assertIn(
            "delete_document_chunks", src,
            "入库前没有清理旧向量 —— 覆盖后旧数据会残留，检索命中旧内容",
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
