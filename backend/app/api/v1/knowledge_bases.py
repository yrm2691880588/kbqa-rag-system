"""知识库管理接口（M2）。

权限设计（多租户版）：
- 查看知识库列表：登录用户均可，但**只能看到自己所属组织的库**（平台超管看全部）；
- 创建/修改/删除知识库、上传/删除文档：仅本组织管理员（后端强校验，不只靠前端隐藏）；
- 取单个知识库一律经过 `core.tenancy.get_scoped_kb`，越权访问返回 404。

⚠️ 改动提醒：
    本文件任何一个"按 kb_id 取库"的地方，都必须走 tenancy 的校验，
    不能直接 `db.get(KnowledgeBase, kb_id)` —— 否则就绕过了租户隔离。
"""

import hashlib
import logging
import os
import shutil

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import AdminUser, CurrentUser, DbSession
from app.core.tenancy import get_scoped_kb
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.schemas.kb import DocOut, KBCreate, KBOut, KBUpdate
from app.services import job_queue, vector_store
from app.services.file_parser import normalize_ext
from app.services.ingestion import doc_file_path, upload_dir_for

logger = logging.getLogger("kbqa.api.kb")

router = APIRouter(prefix="/knowledge-bases", tags=["知识库"])


def _max_upload_mb() -> int:
    """上传大小上限（MB），从配置读（P2-20：不再写死在代码里）。

    做成函数而不是模块级常量，是为了让"改了配置"能立刻生效 ——
    模块级常量在进程启动时就固定了，测试里改配置也影响不到它。
    """
    return int(getattr(settings, "max_upload_mb", 100) or 100)


def _org_id_of(user) -> int:
    """取当前用户所属组织 id；没有归属（极少数情况）则拒绝创建。

    平台超管如果自己也没有组织，是无法创建知识库的 —— 这是有意的：
    知识库必须"有主人"，否则它会变成谁都看不见的孤儿数据。
    超管要建库，先用另一个有组织的管理员账号，或先把自己加进某个组织。
    """
    if user.org_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "当前账号未归属任何组织，无法创建知识库。请先由平台超管将你加入某个组织。",
        )
    return int(user.org_id)


@router.get("", response_model=list[KBOut])
async def list_knowledge_bases(current_user: CurrentUser, db: DbSession, all: bool = Query(False)):
    """知识库列表：只返回当前用户所属组织的库。

    all=1 且为管理员时，可以看到本组织内已停用的库；否则只列启用中的。
    （注意：平台超管也只在"自己的组织"范围内看列表，需要跨组织请用组织管理接口。）
    """
    stmt = select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc())
    # 租户隔离：列表按组织过滤。
    # ⚠️ org_id 为 None 时必须显式返回空，不能依赖 `== None` ——
    # SQLAlchemy 会把它渲染成 `org_id IS NULL`，反而把"无归属的历史库"暴露出来。
    if current_user.org_id is None:
        return []
    stmt = stmt.where(KnowledgeBase.org_id == current_user.org_id)
    if not all or current_user.role != "admin":
        stmt = stmt.where(KnowledgeBase.is_active.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    return rows


@router.post("", response_model=KBOut, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(payload: KBCreate, db: DbSession, admin: AdminUser):
    """新建知识库（管理员），自动归属到本人所在组织。"""
    org_id = _org_id_of(admin)
    # 重名检查限定在"本组织内"：不同客户各有一个「产品手册」是完全正常的
    exists = await db.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.org_id == org_id, KnowledgeBase.name == payload.name
        )
    )
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "本组织内已有同名知识库")
    kb = KnowledgeBase(
        name=payload.name,
        description=payload.description,
        created_by=admin.id,
        org_id=org_id,
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    # 创建上传目录（为后续放文件）
    upload_dir_for(kb.id).mkdir(parents=True, exist_ok=True)
    return kb


@router.put("/{kb_id}", response_model=KBOut)
async def update_knowledge_base(kb_id: int, payload: KBUpdate, db: DbSession, admin: AdminUser):
    """修改知识库简介/启停（本组织管理员）。"""
    kb = await get_scoped_kb(db, admin, kb_id)  # 越权 → 404
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
    """删除知识库：级联清理 Qdrant 向量点、磁盘文件与数据库记录（本组织管理员）。"""
    kb = await get_scoped_kb(db, admin, kb_id)  # 越权 → 404
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
    """某知识库下的全部文档及入库状态（本组织管理员，管理页列表用）。"""
    kb = await get_scoped_kb(db, admin, kb_id)
    stmt = select(Document).where(Document.kb_id == kb.id).order_by(Document.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return rows


@router.post("/{kb_id}/documents", response_model=DocOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    kb_id: int,
    db: DbSession,
    admin: AdminUser,
    file: UploadFile = File(...),
):
    """上传文档并自动触发解析入库（本组织管理员）。支持 docx/pdf/xlsx/txt/md/html。"""
    kb = await get_scoped_kb(db, admin, kb_id)
    if not kb.is_active:
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
    limit_mb = _max_upload_mb()
    if len(content) > limit_mb * 1024 * 1024:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"文件超过 {limit_mb}MB 上限"
            + ("（该上限可由后台配置调整）" if limit_mb else ""),
        )
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

    # 排入解析队列（P2-15）。
    # ⚠️ 这里**用请求自己的数据库会话**（也就是同一个 db 参数），
    #    而不是另起一个后台任务去写 —— 有两个原因：
    #      ① 另起任务会绕开请求的事务：万一文档还没提交，那个任务就找不到文档；
    #      ② 另起的任务用的是"全局连接"，验证脚本没法把数据库重定向到临时库，
    #         结果测试会悄悄写到正式库上去（这个坑本项目已经踩过一次）。
    #    入队只是一次很轻的插入，不会让上传接口变慢；
    #    真正耗时的"解析 + 向量化"仍然由工作进程异步去做。
    await job_queue.enqueue(db, doc.id, kb.id)
    logger.info("文档 %s 已上传并排入解析队列（doc_id=%d）", safe_name, doc.id)
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
