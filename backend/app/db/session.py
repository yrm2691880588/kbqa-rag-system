"""数据库连接与会话管理（异步 SQLAlchemy）。

连接池是干什么的（大白话）：
    和数据库说话要"打电话"，每通电话都要先拨号（建立连接）。
    如果每个请求都重新拨一次号，既慢又费资源。所以做法是：
    预先准备好几条电话线放着，谁来用就占一条，用完立刻还回来。
    这个"放电话线的架子"就是连接池。

    ⚠️ 它有一个很容易被忽略的坑：**电话线数量是有限的**（数据库那边也有上限）。
    如果应用开的线数 × 进程数 超过了数据库允许的总数，
    高峰时就会"拨不出去"—— 表现是接口大面积报错，但平时完全看不出来。
    所以本文件把池大小做成可配置，并在启动自检里**把这道算术题算给运维看**。

另外一件更要命的事：**借了必须还**。
    某个接口如果借了连接忘了还（异常路径最容易漏），池子会一条条被耗干，
    最后全站卡死。所以代码里一律用 `async with`，靠语言机制保证"哪怕抛异常也还回去"。
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings


def _build_engine():
    """根据配置创建异步引擎；SQLite 需要额外的线程参数。

    这里刻意把"配置缺失/写错"翻译成人话再抛错：
    本模块是**导入时**就建连接池的，如果数据库地址没填，
    默认会抛出一大段 SQLAlchemy 内部解析报错——用户根本看不出
    "其实是配置没填"。所以在这里提前拦下来，给出可执行的提示。
    """
    url = (settings.database_url or "").strip()
    if not url:
        raise RuntimeError(
            "数据库连接地址（DATABASE_URL）未配置，服务无法启动。\n"
            "请打开 backend/.env，填写 DATABASE_URL，例如：\n"
            "    DATABASE_URL=postgresql+asyncpg://kbqa:你的口令@postgres:5432/kbqa"
        )

    kwargs: dict = {"pool_pre_ping": settings.db_pool_pre_ping}
    if settings.is_sqlite:
        # SQLite 是单文件数据库，没有"连接池"这回事，只要关掉线程限制即可
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # 池子大小：从配置读，不再用 SQLAlchemy 的默认值
        kwargs.update(
            pool_size=int(settings.db_pool_size or 5),
            max_overflow=int(settings.db_max_overflow or 0),
            pool_timeout=float(settings.db_pool_timeout or 30.0),
            # pool_recycle：连接用超过这个秒数就丢掉重开。
            # 为什么需要：数据库/防火墙/NAT 会悄悄掐掉"太久没说话"的连接，
            # 而应用这边还以为它是好的 —— 下次用就报"连接已被关闭"。
            # 定期换新连接，比等它自己坏掉再重试要省事得多。
            pool_recycle=int(settings.db_pool_recycle_seconds or 1800),
        )

    try:
        return create_async_engine(url, echo=False, **kwargs)
    except Exception as e:  # 地址写错（缺协议头、括号不配对等）
        # 注意：不要把 url 原文写进报错里，它含数据库口令，会进日志
        raise RuntimeError(
            "数据库连接地址（DATABASE_URL）格式不正确，无法解析。\n"
            "请检查 backend/.env 中 DATABASE_URL 的写法，正确格式示例：\n"
            "    DATABASE_URL=postgresql+asyncpg://kbqa:你的口令@postgres:5432/kbqa"
        ) from e


engine = _build_engine()

# 会话工厂：每个请求从这里取一个数据库会话
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def pool_status() -> dict:
    """当前连接池的实时状态（给健康检查用）。

    这是排查"是不是连接被借光忘了还"最直接的一个数：
      · checked_out 一直贴着上限下不来 → 有地方借了没还；
      · 健康检查返回它，就能在监控上看趋势，而不是等用户报"网站打不开了"。
    """
    try:
        pool = engine.pool
        return {
            "size": pool.size(),                    # 池里常备几条
            "checked_out": pool.checkedout(),       # 正在被使用几条（关键数字）
            "overflow": pool.overflow(),            # 临时超出常备数量几条
            "available": max(0, pool.size() - pool.checkedout()),  # 还能借几条
        }
    except Exception:
        # SQLite 等没有标准池实现的场景：拿不到就返回空，绝不因为"看不了状态"把健康检查搞挂
        return {}


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：为每个请求提供独立数据库会话，用完自动关闭。

    用 `async with` 而不是手动 open/close：这样即使接口里抛了异常，
    连接也一定会还回池里（手动 close 最容易在异常分支被漏掉，
    而漏上几次，池子就干了）。
    """
    async with AsyncSessionLocal() as session:
        yield session
