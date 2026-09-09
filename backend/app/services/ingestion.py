"""入库管线编排：文档解析 → 切块 → 向量化 → 写入 Qdrant → 更新状态。

- 每个文档一个独立后台任务（asyncio.create_task），不阻塞网页请求；
- 文档表自带状态机（pending→parsing→embedding→ready/failed）+ progress 百分比，
  前端轮询即可看到实时进度；
- 任一步失败都记录 error_msg 并置 failed，可一键"重试"（重新入队）；
- 向量化按配置批量进行，每批完成后刷新进度；全部向量化完再批量写 Qdrant，
  崩溃重试时先删除该文档旧向量点再写，保证不残留脏数据。
"""

import asyncio
import logging
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.services import vector_store
from app.services.chunker import chunk_parse_result
from app.services.file_parser import normalize_ext, parse_document
from app.services.vectorizer import embed_texts

logger = logging.getLogger("kbqa.ingestion")

# backend 根目录（app/services 的上级的上级）
BACKEND_DIR = Path(__file__).resolve().parents[2]
UPLOAD_ROOT = BACKEND_DIR / settings.upload_dir

# 正在运行的任务：doc_id -> asyncio.Task（防止重复触发）
_running_tasks: dict[int, asyncio.Task] = {}


def doc_file_path(kb_id: int, doc_id: int, ext: str) -> Path:
    """上传文件在磁盘的存放路径：{upload_root}/{kb_id}/{doc_id}.{ext}（用数字命名避开中文路径坑）。"""
    return UPLOAD_ROOT / str(kb_id) / f"{doc_id}.{ext}"


def upload_dir_for(kb_id: int) -> Path:
    """某知识库的上传目录。"""
    return UPLOAD_ROOT / str(kb_id)


async def _load_doc(doc_id: int) -> tuple[Document, KnowledgeBase]:
    """取文档及其所属知识库（独立会话）。"""
    async with AsyncSessionLocal() as db:
        doc = await db.get(Document, doc_id)
        if doc is None:
            raise FileNotFoundError(f"文档 {doc_id} 不存在")
        kb = await db.get(KnowledgeBase, doc.kb_id)
        if kb is None:
            raise FileNotFoundError(f"知识库 {doc.kb_id} 不存在")
        return doc, kb


async def _set_status(doc_id: int, status: str, progress: int | None = None, error: str | None = None, chunk_count: int | None = None) -> None:
    """更新文档状态字段（独立小会话，避免长事务占用）。"""
    async with AsyncSessionLocal() as db:
        doc = await db.get(Document, doc_id)
        if doc is None:
            return
        doc.status = status
        if progress is not None:
            doc.progress = progress
        if error is not None:
            doc.error_msg = error
        if chunk_count is not None:
            doc.chunk_count = chunk_count
        await db.commit()


async def _refresh_kb_stats(kb_id: int) -> None:
    """刷新知识库冗余计数（文档数取表统计，片段数取 Qdrant 精确统计）。"""
    async with AsyncSessionLocal() as db:
        kb = await db.get(KnowledgeBase, kb_id)
        if kb is None:
            return
        doc_cnt = await db.scalar(select(func.count()).select_from(Document).where(Document.kb_id == kb_id))
        kb.doc_count = doc_cnt or 0
        await db.commit()
    chunk_cnt = await vector_store.count_kb_chunks(kb_id)
    async with AsyncSessionLocal() as db:
        kb = await db.get(KnowledgeBase, kb_id)
        if kb is not None:
            kb.chunk_count = chunk_cnt
            await db.commit()


async def process_document(doc_id: int) -> None:
    """单文档入库主流程。所有异常在此捕获并标记 failed，绝不向上抛导致任务崩溃无声。"""
    try:
        doc, kb = await _load_doc(doc_id)
        ext = normalize_ext(doc.filename)
        if ext is None:
            raise ValueError(f"不支持的文件类型：{doc.filename}")

        await _set_status(doc_id, "parsing", progress=1)

        # 1) 解析
        path = doc_file_path(doc.kb_id, doc_id, ext)
        if not path.exists():
            raise FileNotFoundError(f"磁盘文件缺失：{path.name}（请重新上传）")
        logger.info("开始解析文档 %s", doc.filename)
        parse_result = parse_document(str(path), ext)
        if not parse_result.units or not any(u.text.strip() for u in parse_result.units):
            raise ValueError("文档解析后没有提取到任何文字内容")
        if parse_result.warnings:
            logger.warning("解析 %s 提示：%s", doc.filename, "; ".join(parse_result.warnings))

        # 2) 切块
        chunks = chunk_parse_result(parse_result, doc.filename)
        if not chunks:
            raise ValueError("切块结果为空")
        total = len(chunks)
        logger.info("文档 %s 切出 %d 个片段", doc.filename, total)

        # 3) 分批向量化（每批更新一次进度）
        await _set_status(doc_id, "embedding", progress=2)
        texts = [c.text for c in chunks]
        vectors: list[list[float]] = []
        batch_size = settings.embedding_batch_size
        for start in range(0, total, batch_size):
            batch_texts = texts[start : start + batch_size]
            try:
                vecs = await embed_texts(batch_texts)
            except Exception as e:
                raise RuntimeError(f"向量化第 {start // batch_size + 1} 批失败：{e}") from e
            vectors.extend(vecs)
            done = min(start + batch_size, total)
            progress = max(2, int(done / total * 95))  # 向量化阶段封顶 95%
            await _set_status(doc_id, "embedding", progress=progress)

        # 4) 先清旧向量点（重试/覆盖场景），再整批写入 Qdrant
        await vector_store.delete_document_chunks(doc_id)
        await vector_store.ensure_collection()
        await vector_store.upsert_document_chunks(
            kb_id=doc.kb_id,
            doc_id=doc_id,
            doc_name=doc.filename,
            vectors=vectors,
            texts=texts,
            pages=[c.page for c in chunks],
        )

        # 5) 完成
        await _set_status(doc_id, "ready", progress=100, error=None, chunk_count=total)
        await _refresh_kb_stats(doc.kb_id)
        logger.info("文档 %s 入库完成（%d 片段）", doc.filename, total)
    except Exception as e:
        logger.exception("文档 %d 入库失败", doc_id)
        await _set_status(doc_id, "failed", error=str(e)[:2000])
    finally:
        _running_tasks.pop(doc_id, None)


def start_ingestion(doc_id: int) -> None:
    """为文档创建一个后台入库任务；已存在同文档任务则跳过。"""
    if doc_id in _running_tasks and not _running_tasks[doc_id].done():
        return
    task = asyncio.create_task(process_document(doc_id))
    _running_tasks[doc_id] = task


def is_ingesting(doc_id: int) -> bool:
    """该文档是否正在入库。"""
    t = _running_tasks.get(doc_id)
    return bool(t and not t.done())
