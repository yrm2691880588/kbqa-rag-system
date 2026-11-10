"""文档级操作接口（M2）：删除 / 重试入库 / 分块预览 / 向量服务健康检查。

全部仅管理员可访问（多租户语境下即"本组织管理员"）。

⚠️ 租户隔离要点：
    文档本身没有 org_id，"归属"由它所属的知识库决定。所以这里所有按 doc_id 取文档的地方，
    都必须**顺藤摸瓜校验它所属知识库的组织**，否则任何组织的管理员只要猜一个 doc_id
    就能读写别家客户的文档（这是真实存在过的漏洞，已修复）。
"""

import logging

from fastapi import APIRouter, HTTPException, status

from app.core.deps import AdminUser, DbSession
from app.core.tenancy import get_scoped_kb
from app.models.document import Document
from app.schemas.kb import ChunkItem, DocOut, EmbeddingHealth
from app.services import vector_store
from app.services.file_parser import normalize_ext
from app.services import job_queue
from app.services.ingestion import doc_file_path
from app.services.ingestion import _refresh_kb_stats
from app.services.vectorizer import health_check as embedding_health_check

logger = logging.getLogger("kbqa.api.doc")

router = APIRouter(tags=["文档"])


async def _get_doc(db, doc_id: int, admin) -> Document:
    """取文档，并确保它属于当前管理员可访问的知识库；否则 404。

    get_scoped_kb 会在知识库不属于本组织时抛 404，从而拦住跨租户访问。
    """
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    await get_scoped_kb(db, admin, doc.kb_id)  # 越权 → 404
    return doc


@router.get("/documents/{doc_id}", response_model=DocOut)
async def get_document(doc_id: int, db: DbSession, admin: AdminUser):
    """单文档详情与入库状态（前端轮询进度用）。"""
    return await _get_doc(db, doc_id, admin)


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(doc_id: int, db: DbSession, admin: AdminUser):
    """删除文档：先清 Qdrant 向量点，再删磁盘文件与数据库记录。"""
    doc = await _get_doc(db, doc_id, admin)
    kb_id = doc.kb_id
    # 若正在入库，等待任务结束后由任务自己清理状态；这里直接标记（任务失败会写入 failed，无碍）
    await vector_store.delete_document_chunks(doc_id)
    try:
        ext = normalize_ext(doc.filename)
        if ext:
            p = doc_file_path(kb_id, doc_id, ext)
            if p.exists():
                p.unlink()
    except OSError:
        logger.warning("删除磁盘文件失败 doc_id=%d", doc_id)
    await db.delete(doc)
    await db.commit()
    # 刷新知识库的文档数/片段数冗余计数（删除后保持一致）
    await _refresh_kb_stats(kb_id)
    logger.info("文档 %d 已删除", doc_id)


@router.post("/documents/{doc_id}/retry", response_model=DocOut)
async def retry_document(doc_id: int, db: DbSession, admin: AdminUser):
    """入库失败的文档一键重试：重置状态后重新排入解析队列。"""
    doc = await _get_doc(db, doc_id, admin)
    if doc.status != "failed":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有入库失败的文档可以重试")
    # 判断"是不是已经在处理中"要查**数据库里的任务表**，不能用内存里的字典：
    # 多进程部署时，A 进程看不到 B 进程正在处理的文档，就会造成同一个文档被解析两遍。
    if await job_queue.find_active(db, doc_id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "该文档正在处理中")
    doc.status = "pending"
    doc.progress = 0
    doc.error_msg = None
    db.add(doc)
    await db.commit()
    # 重新入队：上一条任务已经处于 failed（重试次数用完）状态，
    # 所以幂等判断不会把它当成"活跃任务"，会正常生成一条新任务。
    await job_queue.enqueue(db, doc_id, doc.kb_id)
    await db.refresh(doc)
    return doc


@router.get("/documents/{doc_id}/chunks", response_model=list[ChunkItem])
async def document_chunks(doc_id: int, db: DbSession, admin: AdminUser):
    """分块预览：列出该文档入库后的全部知识片段（从 Qdrant 读取）。"""
    doc = await _get_doc(db, doc_id, admin)
    items = await vector_store.list_document_chunks(doc_id, limit=500)
    if not items and doc.status == "ready":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "向量库中未找到该文档的片段（可能数据异常）")
    return items


@router.get("/ai/embedding-health", response_model=EmbeddingHealth)
async def embedding_health(admin: AdminUser):
    """向量服务健康检查：调一次百炼接口确认 Key/网络正常（管理员页面展示）。"""
    ok, message = await embedding_health_check()
    return EmbeddingHealth(ok=ok, message=message)
