"""入库管线编排：文档解析 → 切块 → 向量化 → 写入 Qdrant → 更新状态。

- 每个文档对应数据库里的一条解析任务（P2-15），由工作进程领取执行，不阻塞网页请求；
  任务可重试、可在重启后恢复，多进程部署也不会重复解析同一个文档；
- 文档表自带状态机（pending→parsing→embedding→ready/failed）+ progress 百分比，
  前端轮询即可看到实时进度；
- 任一步失败都记录 error_msg 并置 failed，可一键"重试"（重新入队）；
- 向量化按配置批量进行，每批完成后刷新进度；全部向量化完再批量写 Qdrant，
  崩溃重试时先删除该文档旧向量点再写，保证不残留脏数据。
"""

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


async def _record_embedding_usage(kb_id: int, chunks: int) -> None:
    """把本次向量化的块数记到知识库所属组织的当日用量上。

    为什么要"顺藤摸瓜"找组织：文档表里没有 org_id，只有 kb_id，
    必须 文档 → 知识库 → 组织 一路查上去才知道这笔账记给谁。
    （这和隔离校验是同一个套路，漏一步就会把用量记到别人头上。）
    """
    from app.db.session import AsyncSessionLocal
    from app.models.knowledge_base import KnowledgeBase
    from app.services import quota as quota_service

    async with AsyncSessionLocal() as db:
        kb = await db.get(KnowledgeBase, kb_id)
        if kb is None:
            return
        await quota_service.add_embedding_chunks(db, kb.org_id, chunks)


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
        # 5.5) 记一笔向量化用量（按文本块数）。
        #      放在这里而不是"每批记一次"：批次的粒度太细，用量表一天会被写几百次，
        #      而额度是"够用就行"，不需要精确到批。失败不影响入库结果——所以吞掉异常。
        try:
            await _record_embedding_usage(doc.kb_id, total)
        except Exception:
            logger.warning("记录向量化用量失败（不影响入库结果）", exc_info=True)
        logger.info("文档 %s 入库完成（%d 片段）", doc.filename, total)
    except Exception as e:
        logger.exception("文档 %d 入库失败", doc_id)
        await _set_status(doc_id, "failed", error=str(e)[:2000])
        # 说明：这里把异常吞掉了（只记状态），因为是"队列里还有重试机会"。
        # 但注意——process_document 是被 job_queue 调用的，
        # 如果这里吞掉异常，队列就会误判"任务成功"，永远不会重试。
        # 所以吞掉之前先**重新抛出去**，让队列来决定是重试还是彻底放弃。
        raise


# 说明：这里曾经有一个 start_ingestion(doc_id)，作用是"在内存里起一个后台任务去解析"。
# P2-15 之后它已被**删除**，改成由接口层调用 job_queue.enqueue(db, doc_id, kb_id) 排队。
#
# 为什么必须删掉而不是留着兼容：
#   它内部用的是"全局数据库连接"（AsyncSessionLocal），绕开了请求自己的会话。留着它，
#   早晚有人图省事又调它，于是又回到"任务留在内存里、进程一重启就丢"的老问题；
#   而且它会让验证脚本无法把数据库重定向到临时库（依赖注入换不掉全局连接），
#   导致测试悄悄写到正式库上 —— 这个坑本项目已经踩过一次。
#   删掉它，"入队"这件事就只有一条路可走，也就不会走错。
