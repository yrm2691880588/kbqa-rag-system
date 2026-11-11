"""知识库管理接口（M2）。

权限设计（多租户版）：
- 查看知识库列表：登录用户均可，但**只能看到自己所属组织的库**。
  注意：**平台超管也不例外** —— 列表接口对他的范围同样是"自己的组织"，
  需要跨组织请用组织管理接口（/organizations）。这样设计是为了避免
  "超管随手一刷就看到所有客户资料"这类意外；
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
from app.schemas.kb import (
    BatchUploadFailOut,
    BatchUploadResultOut,
    DocOut,
    KBCreate,
    KBOut,
    KBUpdate,
)
from app.services import job_queue, vector_store
from app.services.file_parser import normalize_ext
from app.services.ingestion import doc_file_path, upload_dir_for

logger = logging.getLogger("kbqa.api.kb")

router = APIRouter(prefix="/knowledge-bases", tags=["知识库"])


# 批量上传一次最多几个文件。
# 定这个上限的原因：防止有人写脚本一次塞几千个文件，
# 把内存和解析队列同时打满（每个文件都要读进内存、都要排一条任务）。
MAX_BATCH_FILES = 50


def _max_upload_mb() -> int:
    """上传大小上限（MB），从配置读，不再写死在代码里。

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


async def _save_one_document(
    kb_id: int,
    kb,
    db,
    file: UploadFile,
) -> Document:
    """保存**单个**上传文件：校验 → 查重 → 落盘 → 入队。

    抽成独立函数的原因：
        单文件接口和批量接口必须走**完全相同**的校验与查重逻辑。
        如果各写一份，迟早出现"批量能传、单个不能传"这种诡异差异。
        这里把流程集中一处，两个接口都调它。

    异常约定：
        校验失败直接抛 HTTPException（4xx），由调用方决定怎么呈现。
        批量接口会捕获它，把单个文件的失败收集成"部分成功"结果，
        而不是让整个批次失败。
    """
    raw_name = (file.filename or "").strip() or "未命名文件"
    # 清洗文件名：去掉路径成分（仅作展示；磁盘文件用 id 命名避开中文/特殊字符）
    safe_name = os.path.basename(raw_name.replace("\\", "/")).strip()[:255] or "未命名文件"
    ext = normalize_ext(safe_name)
    if ext is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "不支持的文件格式，请上传 docx / pdf / xlsx / txt / md / html",
        )

    # 大小限制：**边读边判**，不是读完再判。
    #
    # 为什么必须这样：
    #   早期写法是 `content = await file.read()` 先把整个文件读进内存，
    #   之后才比较大小。传一个 2GB 的文件会先把 2GB 塞进内存 ——
    #   轻则拖慢整个服务，重则 OOM 把进程打死，上限形同虚设。
    #   现在按块读，累计超限立刻 413 并停止读取，内存占用封顶在 limit_mb。
    limit_mb = _max_upload_mb()
    limit_bytes = limit_mb * 1024 * 1024
    too_large_msg = (
        f"文件超过 {limit_mb}MB 上限"
        + ("（该上限可由后台配置调整）" if limit_mb else "")
    )

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)   # 每次读 1MB
        if not chunk:
            break
        total += len(chunk)
        if total > limit_bytes:
            # 立即停止，不再继续读；已读的部分随请求结束释放
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, too_large_msg
            )
        chunks.append(chunk)

    content = b"".join(chunks)
    chunks.clear()   # 及时释放分块列表，避免同时持有两份内存

    if len(content) == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件内容为空")
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

    # 排入解析队列。
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


@router.post("/{kb_id}/documents", response_model=DocOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    kb_id: int,
    db: DbSession,
    admin: AdminUser,
    file: UploadFile = File(...),
):
    """上传单个文档并自动触发解析入库（本组织管理员）。

    支持 docx / pdf / xlsx / txt / md / html。
    需要一次传多个文件时用 /documents/batch。
    """
    kb = await get_scoped_kb(db, admin, kb_id)
    if not kb.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在或已停用")
    return await _save_one_document(kb_id, kb, db, file)


@router.post("/{kb_id}/documents/batch", response_model=BatchUploadResultOut)
async def upload_documents_batch(
    kb_id: int,
    db: DbSession,
    admin: AdminUser,
    files: list[UploadFile] = File(..., description="要上传的多个文件"),
):
    """批量上传文档（本组织管理员）。

    为什么单独做一个批量接口，而不是让前端循环调单文件接口：
      ① 用户一次拖 50 个文件时，50 次串行请求的总耗时更长，
         而且中途断网会留下"传了一半"的状态；
      ② 服务端一次处理能控制并发与内存（逐个处理，不并发读所有文件）；
      ③ 返回结构能统一给出"哪几个成功、哪几个失败、为什么失败"。

    为什么是"部分成功"语义而不是"全有或全无"：
      用户拖进来的文件里可能夹着一个不支持的格式。
      如果整批回滚，用户得自己找是哪个文件的问题，体验很差。
      所以这里逐个处理，成功的照常入库，失败的收集原因返回。
    """
    kb = await get_scoped_kb(db, admin, kb_id)
    if not kb.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在或已停用")

    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有收到文件")

    # 一次最多传多少个：防止有人用脚本塞几千个文件把服务拖垮
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"一次最多上传 {MAX_BATCH_FILES} 个文件（当前 {len(files)} 个），请分批上传",
        )

    succeeded: list[DocOut] = []
    failed: list[BatchUploadFailOut] = []

    # ⚠️ 每个文件用**独立的会话**处理，而不是共用请求的 db。
    #
    # 为什么必须这样（实测踩过的坑）：
    #   最初实现在失败时调 db.rollback() 想让"这一个文件"回滚，
    #   但 rollback 是**整个会话级**的 —— 它把同一会话里前面已经成功的文件
    #   也一起回滚了，后续文件还会因为会话状态错乱被误报成"服务内部错误"。
    #   实测结果：3 个文件（2 个正常 + 1 个坏格式）只有 1 个成功。
    #
    #   改成每个文件独立会话后，失败只回滚自己，互不影响，
    #   "部分成功"语义才真正成立。
    from app.db.session import AsyncSessionLocal

    for f in files:
        name = (f.filename or "未命名文件").strip()
        try:
            async with AsyncSessionLocal() as per_file_db:
                doc = await _save_one_document(kb_id, kb, per_file_db, f)
                # 会话关闭前把需要的字段读出来，避免 detached 后再访问触发懒加载
                doc_out = DocOut.model_validate(doc)
            succeeded.append(doc_out)
        except HTTPException as exc:
            # 单个文件失败不影响其他文件（见 docstring 的"部分成功"说明）
            failed.append(
                BatchUploadFailOut(filename=name, reason=str(exc.detail))
            )
        except Exception:
            logger.exception("批量上传时文件 %s 处理异常", name)
            failed.append(BatchUploadFailOut(filename=name, reason="服务内部错误"))

    logger.info(
        "批量上传完成：成功 %d 个，失败 %d 个（kb_id=%d）",
        len(succeeded), len(failed), kb_id,
    )
    return BatchUploadResultOut(
        total=len(files),
        succeeded=len(succeeded),
        failed=len(failed),
        documents=succeeded,
        failures=failed,
    )


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
