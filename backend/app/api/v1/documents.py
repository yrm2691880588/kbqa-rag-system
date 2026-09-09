"""文档级操作接口（M2）：删除 / 重试入库 / 分块预览 / 向量服务健康检查。

全部仅管理员可访问。删除文档会级联清理 Qdrant 向量点与磁盘原文件。
"""

import logging

from fastapi import APIRouter, HTTPException, status

from app.core.deps import AdminUser, DbSession
from app.models.document import Document
from app.schemas.kb import ChunkItem, DocOut, EmbeddingHealth
from app.services import vector_store
from app.services.file_parser import normalize_ext
from app.services.ingestion import doc_file_path, is_ingesting, start_ingestion
from app.services.ingestion import _refresh_kb_stats
from app.services.vectorizer import health_check as embedding_health_check

logger = logging.getLogger("kbqa.api.doc")

router = APIRouter(tags=["文档"])


async def _get_doc(db, doc_id: int) -> Document:
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    return doc


@router.get("/documents/{doc_id}", response_model=DocOut)
async def get_document(doc_id: int, db: DbSession, admin: AdminUser):
    """单文档详情与入库状态（前端轮询进度用）。"""
    return await _get_doc(db, doc_id)


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(doc_id: int, db: DbSession, admin: AdminUser):
    """删除文档：先清 Qdrant 向量点，再删磁盘文件与数据库记录。"""
    doc = await _get_doc(db, doc_id)
    # 若正在入库，等待任务结束后由任务自己清理状态；这里直接标记（任务失败会写入 failed，无碍）
    await vector_store.delete_document_chunks(doc_id)
    try:
        ext = normalize_ext(doc.filename)
        if ext:
            p = doc_file_path(doc.kb_id, doc_id, ext)
            if p.exists():
                p.unlink()
    except OSError:
        logger.warning("删除磁盘文件失败 doc_id=%d", doc_id)
    await db.delete(doc)
    await db.commit()
    # 刷新知识库的文档数/片段数冗余计数（删除后保持一致）
    await _refresh_kb_stats(doc.kb_id)
    logger.info("文档 %d 已删除", doc_id)


@router.post("/documents/{doc_id}/retry", response_model=DocOut)
async def retry_document(doc_id: int, db: DbSession, admin: AdminUser):
    """入库失败的文档一键重试：重置状态后重新触发后台任务。"""
    doc = await _get_doc(db, doc_id)
    if doc.status != "failed":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只有入库失败的文档可以重试")
    if is_ingesting(doc_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "该文档正在处理中")
    doc.status = "pending"
    doc.progress = 0
    doc.error_msg = None
    db.add(doc)
    await db.commit()
    start_ingestion(doc_id)
    await db.refresh(doc)
    return doc


@router.get("/documents/{doc_id}/chunks", response_model=list[ChunkItem])
async def document_chunks(doc_id: int, db: DbSession, admin: AdminUser):
    """分块预览：列出该文档入库后的全部知识片段（从 Qdrant 读取）。"""
    doc = await _get_doc(db, doc_id)
    items = await vector_store.list_document_chunks(doc_id, limit=500)
    if not items and doc.status == "ready":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "向量库中未找到该文档的片段（可能数据异常）")
    return items


@router.get("/ai/embedding-health", response_model=EmbeddingHealth)
async def embedding_health(admin: AdminUser):
    """向量服务健康检查：调一次百炼接口确认 Key/网络正常（管理员页面展示）。"""
    ok, message = await embedding_health_check()
    return EmbeddingHealth(ok=ok, message=message)
