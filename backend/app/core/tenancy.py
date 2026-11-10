"""租户隔离的唯一权威入口（多租户安全的核心）。

⚠️ 改动本文件前请先读完这段说明。

设计原则（为什么这么写）：
    1. **前端传来的知识库 id 一律不可信**。
       用户完全可以自己改请求体，把别家客户的知识库 id 塞进来。
       所以任何地方要用 kb_ids，都必须先经过本模块的 resolve_kb_scope 收敛 ——
       收敛后的结果才是"这个人真正能看的东西"。
    2. **隔离判断只有这一处实现**。
       如果在每个接口里各写一遍"判断组织是否相同"，写十遍就有十次漏的机会；
       集中在一处，改一次全局生效，也只需要为这一处写测试。
    3. **空列表 ≠ 不限制**。
       `None` 表示"不加限制"，`[]` 表示"一个库都不能看"。
       两者混用会导致跨客户串数据（这个坑本项目真实踩过一次，见 vector_store.search 的注释）。
       所以本模块对普通用户**永远不返回 None**：哪怕一个库都没有，也返回空列表。

角色与可见范围：
    平台超管（is_superadmin=True）→ 不受组织限制（None 或指定库）
    组织管理员 / 普通成员        → 只能看到自己所属组织的知识库
"""

import logging

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import KnowledgeBase
from app.models.user import User

logger = logging.getLogger("kbqa.tenancy")


def is_unrestricted(user: User) -> bool:
    """是否为不受组织限制的账号（平台超管）。"""
    return bool(getattr(user, "is_superadmin", False))


async def accessible_kb_ids(db: AsyncSession, user: User) -> list[int]:
    """当前用户**有权访问**的全部知识库 id（含已停用的，用于权限判断而非展示）。

    普通用户：只列本组织的库。
    平台超管：列全部库。
    """
    if not is_unrestricted(user):
        # ⚠️ 这里必须显式短路，不能直接写 `org_id == user.org_id`。
        # 原因：SQLAlchemy 会把 `列 == None` 渲染成 `org_id IS NULL`，
        # 于是"没归属组织的账号"反而能查到**所有 org_id 为 NULL 的历史库**——
        # 与"什么也看不到"的意图正好相反，构成越权。
        # users.org_id 外键是 ondelete=SET NULL，组织被删后账号就会落到这个分支。
        if user.org_id is None:
            return []
        stmt = select(KnowledgeBase.id).where(KnowledgeBase.org_id == user.org_id)
    else:
        stmt = select(KnowledgeBase.id)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def resolve_kb_scope(
    db: AsyncSession, user: User, requested: list[int] | None
) -> list[int] | None:
    """把"前端传来的检索范围"收敛成"该用户真正可用的范围"。

    Args:
        requested: 前端传来的知识库 id 列表；None 或空列表表示"不指定、用全部"。

    Returns:
        - 平台超管：requested 为空 → None（不限制）；否则原样返回去重后的列表
        - 普通用户：一定返回列表（可能是空列表）。
          空列表代表"这个用户什么库都不能看"，下游必须据此返回"无结果"，
          **绝不能**把它当成"不限制"。

    越权的 id 会被**静默丢弃**：不报错、不提示，避免泄露"别家客户存在某个库"这一信息。
    """
    requested = [int(x) for x in (requested or [])]

    if is_unrestricted(user):
        return sorted(set(requested)) if requested else None

    allowed = set(await accessible_kb_ids(db, user))
    if not requested:
        # 没指定库 → 用他有权访问的全部库（可能为空列表，含义是"什么都搜不到"）
        return sorted(allowed)

    kept = sorted(allowed & set(requested))
    if len(kept) != len(set(requested)):
        # 记录但不报错：这是有人在试探越权访问的迹象，值得留痕
        logger.warning(
            "用户 %s(id=%s, org=%s) 请求了非本组织知识库，已剔除越权部分：请求=%s 允许=%s",
            user.username, user.id, user.org_id, sorted(set(requested)), kept,
        )
    return kept


async def get_scoped_kb(db: AsyncSession, user: User, kb_id: int) -> KnowledgeBase:
    """取指定知识库，并确保它在当前用户的可见范围内；否则 404。

    为什么越权返回 404 而不是 403：
        403 等于告诉对方"这个 id 是存在的，只是你没权限"，相当于泄露了别家资源的存在。
        404 则跟"不存在"无法区分，信息更少。
    """
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or not is_unrestricted(user) and kb.org_id != user.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "知识库不存在")
    return kb


# ============================================================
# 按「组织」而不是按「人」收敛范围（P1-G 对外 API 用）
# ============================================================
#
# 为什么需要单独一组函数：
#     对外 API 的调用方是一个 API Key，它代表**一家客户**，背后没有具体的人。
#     如果为了省事，随便造一个"假用户"塞进上面那两个函数，就等于在隔离逻辑上
#     开了一个"绕过用户身份"的口子——将来谁都能仿照着造用户绕过校验。
#     所以在这里显式补一组"以组织为主体"的函数，语义清楚：
#     它的可见范围**永远**等于该组织的知识库，不存在"不受限"这种可能
#     （企业客户的 Key 绝不能看到别家客户的库）。
#
# 注意：这一组**不会**返回 None。
#     上面的 resolve_kb_scope 里 None 表示"不限制"，那是留给平台超管的；
#     API Key 永远不该有"不限制"这种状态，所以这里一律返回列表（可能为空）。


async def accessible_kb_ids_for_org(db: AsyncSession, org_id: int | None) -> list[int]:
    """某个组织**有权访问**的全部知识库 id。

    org_id 为 None（不该出现，但防御一下）→ 返回空列表，即"什么都看不到"。
    这里绝不返回"全部"，因为无法归属的请求不应该有任何数据权限。
    """
    if org_id is None:
        return []
    stmt = select(KnowledgeBase.id).where(KnowledgeBase.org_id == org_id)
    return list((await db.execute(stmt)).scalars().all())


async def resolve_kb_scope_for_org(
    db: AsyncSession, org_id: int | None, requested: list[int] | None
) -> list[int]:
    """把 API 请求里传来的检索范围，收敛成"该组织真正可用的范围"。

    与 resolve_kb_scope 的行为对齐（越权的 id 静默丢弃、不报错），
    区别只有一个：**永远返回列表，永不返回 None**。
    """
    allowed = set(await accessible_kb_ids_for_org(db, org_id))
    requested = [int(x) for x in (requested or [])]
    if not requested:
        return sorted(allowed)
    kept = sorted(allowed & set(requested))
    if len(kept) != len(set(requested)):
        logger.warning(
            "组织 %s 的 API 调用请求了非本组织知识库，已剔除越权部分：请求=%s 允许=%s",
            org_id, sorted(set(requested)), kept,
        )
    return kept
