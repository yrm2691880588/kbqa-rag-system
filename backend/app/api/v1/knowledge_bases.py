"""知识库管理接口（M2）。

权限设计：
- 查看知识库列表：登录用户均可（M3 问答时要选库）；
- 创建/修改/删除知识库、上传/删除文档：仅管理员（require_admin，后端强校验，不只靠前端隐藏）。
"""

import hashlib
import logging
import os
import shutil

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select

from app.core.deps import AdminUser, CurrentUser, DbSession
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.schemas.kb import DocOut, KBCreate, KBOut, KBUpdate
from app.services import vector_store
from app.services.file_parser import normalize_ext
from app.services.ingestion import doc_file_path, start_ingestion, upload_dir_for

logger = logging.getLogger("kbqa.api.kb")

router = APIRouter(prefix="/knowledge-bases", tags=["知识库"])

# 上传大小上限：100 MB
MAX_UPLOAD_MB = 100


@router.get("", response_model=list[KBOut])
async def list_knowledge_bases(current_user: CurrentUser, db: DbSession, all: bool = Query(False)):
    """知识库列表：管理页传 all=1 看全部；问答页默认只列启用中的库。"""
    stmt = select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc())
    if not all or current_user.role != "admin":
        stmt = stmt.where(KnowledgeBase.is_active.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    return rows


@router.post("", response_model=KBOut, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(payload: KBCreate, db: DbSession, admin: AdminUser):
    """新建知识库（管理员）。"""
    exists = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == payload.name))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "同名知识库已存在")
    kb = KnowledgeBase(name=payload.name, description=payload.description, created_by=admin.id)
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    # 创建上传目录（为后续放文件）
    upload_dir_for(kb.id).mkdir(parents=True, exist_ok=True)
    return kb


@router.put("/{kb_id}", response_model=KBOut)
async def update_knowledge_base(kb_id: int, payload: KBUpdate, db: DbSession, admin: AdminUser):
    """修改知识库简介/启停（管理员）。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    if payload.description is not None:
        kb.description = payload.description
    if payload.is_active is not None:
        kb.is_active = payload.is_active
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(kb_id: int, db: DbSession, admin: AdminUser):
    """删除知识库：级联清理 Qdrant 向量点、磁盘文件与数据库记录（管理员）。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    # 1) 清向量库中该库的全部向量点
    await vector_store.delete_kb_chunks(kb_id)
    # 2) 删磁盘上传目录（连同所有已上传文件）
    d = upload_dir_for(kb_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    # 3) 删数据库记录（documents 由外键 ON DELETE CASCADE 一并清除）
    await db.delete(kb)
    await db.commit()
    logger.info("知识库 %s（id=%d）已删除", kb.name, kb_id)


# ---------------------------------------------------------------- 文档

@router.get("/{kb_id}/documents", response_model=list[DocOut])
async def list_documents(kb_id: int, db: DbSession, admin: AdminUser):
    """某知识库下的全部文档及入库状态（管理员，管理页列表用）。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    stmt = select(Document).where(Document.kb_id == kb_id).order_by(Document.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return rows


@router.post("/{kb_id}/documents", response_model=DocOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    kb_id: int,
    db: DbSession,
    admin: AdminUser,
    file: UploadFile = File(...),
):
    """上传文档并自动触发解析入库（管理员）。支持 docx/pdf/xlsx/txt/md/html。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or not kb.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在或已停用")

    raw_name = (file.filename or "").strip() or "未命名文件"
    # 清洗文件名：去掉路径成分（仅作展示；磁盘文件用 id 命名避开中文/特殊字符）
    safe_name = os.path.basename(raw_name.replace("\\", "/")).strip()[:255] or "未命名文件"
    ext = normalize_ext(safe_name)
    if ext is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "不支持的文件格式，请上传 docx / pdf / xlsx / txt / md / html",
        )

    # 读内容 + 大小限制 + 内容哈希（去重用）
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件内容为空")
    if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"文件超过 {MAX_UPLOAD_MB}MB 上限")
    file_hash = hashlib.sha256(content).hexdigest()

    # 同知识库内查重：内容完全相同的文件
    exist = await db.scalar(
        select(Document).where(Document.kb_id == kb_id, Document.file_hash == file_hash)
    )
    if exist:
        if exist.status == "ready":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "该文件已在知识库中（如需更新内容，请先删除旧文件再上传）",
            )
        if exist.status in ("pending", "parsing", "embedding"):
            raise HTTPException(status.HTTP_409_CONFLICT, "该文件正在入库处理中，请稍候")
        # 旧记录是 failed：删掉旧记录与磁盘文件，按新上传处理（相当于重试）
        _remove_doc_file(exist)
        await db.delete(exist)
        await db.flush()

    # 建记录 → 落盘 → 触发后台入库
    doc = Document(
        kb_id=kb_id,
        filename=safe_name,
        file_hash=file_hash,
        file_size=len(content),
        doc_type=ext,
        status="pending",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    d = upload_dir_for(kb_id)
    d.mkdir(parents=True, exist_ok=True)
    p = doc_file_path(kb_id, doc.id, ext)
    with open(p, "wb") as f:
        f.write(content)

    start_ingestion(doc.id)  # 后台解析入库，进度在文档状态里实时可查
    logger.info("文档 %s 已上传并开始入库（doc_id=%d）", safe_name, doc.id)
    return doc


def _remove_doc_file(doc: Document) -> None:
    """删除某文档的磁盘文件（尽力而为，不存在也忽略）。"""
    try:
        ext = normalize_ext(doc.filename)
        if ext:
            p = doc_file_path(doc.kb_id, doc.id, ext)
            if p.exists():
                p.unlink()
    except OSError:
        logger.warning("删除文档磁盘文件失败：doc_id=%d", doc.id)
