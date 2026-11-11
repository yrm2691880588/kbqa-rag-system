"""额度与用量服务：管住"客户能用多少"，并如实记账。

要解决的两个真问题：
    ① **成本裸奔**：没有上限时，一个被泄露的账号（或者一个恶意用户）
       可以整夜不停地提问，大模型按次收费，账单会一直涨到天亮。
    ② **没法收费**：要按用量卖，得先有可信的用量数据。

设计上的三个取舍，都写在下面（不写清楚，半年后自己都会怀疑）

取舍一：**先扣后用**（提问一进来就记 1 次），而不是"答完再记"。
    好处：并发下不会超额——10 个请求同时进来，看到"还剩 1 次"时都放行，
          答完再记就已经超了 9 次。先扣的话，第 2 个请求就会被拦。
    代价：这个请求最后如果生成失败，这一次也算掉了。
        对客户"少算一次"比"被刷穿"重要得多，所以接受这个代价。

取舍二：**按天聚合存一行**，不存明细。
    明细在 query_logs 里已经有了（谁、问了什么、耗时多久），
    这张表只负责"快速算额度"，不需要重复存一份。

取舍三：**每日额度用服务器本地日期**，不用 UTC。
    中国客户的"每天"就是北京时间零点，用 UTC 会变成早上 8 点重置，
    客户会觉得"这系统怎么半夜扣我额度"。服务器时区设成 Asia/Shanghai 即可。

限额字段为 None = **不限**。注意区分 None 和 0：
    None → 不限（合同没写次数）
    0    → 一次都不允许（等于停用，但保留组织数据）
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_quota import OrgQuota
from app.models.usage_daily import UsageDaily

logger = logging.getLogger("kbqa.quota")


def today_local() -> dt.date:
    """今天（服务器本地日期）。额度按这个日期滚动。"""
    return dt.date.today()


def month_start_local() -> dt.date:
    """本月第一天（服务器本地日期）。"""
    t = today_local()
    return t.replace(day=1)


@dataclass
class QuotaDecision:
    """一次额度判定的结果。"""

    allowed: bool
    reason: str = ""          # 不允许时给用户看的中文原因
    status_code: int = 200    # 建议的 HTTP 状态码（402=要付费 / 429=太频繁）
    # 便于接口把它顺带告诉前端："你今天还剩多少次"
    daily_used: int = 0
    daily_limit: int | None = None
    monthly_used: int = 0
    monthly_limit: int | None = None


# ============================================================
# 额度读取
# ============================================================

async def get_quota(db: AsyncSession, org_id: int | None) -> OrgQuota | None:
    """取某组织的额度配置；没配过返回 None（等价于"不限"）。"""
    if org_id is None:
        return None
    return await db.scalar(select(OrgQuota).where(OrgQuota.org_id == org_id))


async def get_or_create_quota(db: AsyncSession, org_id: int) -> OrgQuota:
    """取额度配置，没有就建一条"全部不限"的默认配置。

    为什么默认是不限而不是给一个具体数字：
        凭空给个数字（比如"每天 100 次"）会让老客户在升级后突然被拦住，
        这是很讨厌的意外。默认放开、由运营显式设置，行为才是可预期的。
    """
    q = await get_quota(db, org_id)
    if q is not None:
        return q
    q = OrgQuota(org_id=org_id, note="系统自动创建：默认不限量")
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return q


# ============================================================
# 用量写入（原子累加）
# ============================================================

async def _bump(
    db: AsyncSession,
    org_id: int,
    day: dt.date,
    *,
    questions: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    embedding_chunks: int = 0,
) -> None:
    """把今天的用量往上加。已经有一行就加，没有就新建。

    实现要点：用 SQL 自带的 "字段 = 字段 + n" 来累加，而不是
    "先读出来、在 Python 里加、再写回去"。
    后者在并发下会丢计数（两个请求同时读到 5，各自写回 6，实际应该是 7）。
    这里的写法在数据库层面完成，天然不会丢。
    """
    deltas = {
        "questions": questions,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "embedding_chunks": embedding_chunks,
    }
    nonzero = {k: v for k, v in deltas.items() if v}
    if not nonzero:
        return

    values = {k: getattr(UsageDaily, k) + v for k, v in nonzero.items()}
    res = await db.execute(
        update(UsageDaily)
        .where(UsageDaily.org_id == org_id, UsageDaily.day == day)
        .values(**values)
    )
    if res.rowcount > 0:
        await db.commit()
        return

    # 今天还没有记录 → 新建一行。
    # 注意：唯一约束的冲突是在 **提交那一刻** 才抛出来的，不是在 add() 的时候，
    # 所以必须把 commit 也放进 try 里，否则并发冲突会直接冒到接口层变成 500。
    try:
        db.add(UsageDaily(org_id=org_id, day=day, **nonzero))
        await db.commit()
    except IntegrityError:
        # 刚好被别人抢先插入了：回退成"累加"。
        # 这一步不能省 —— 省掉就等于把这笔用量静默丢掉，日积月累账就对不上了。
        await db.rollback()
        await db.execute(
            update(UsageDaily)
            .where(UsageDaily.org_id == org_id, UsageDaily.day == day)
            .values(**values)
        )
        await db.commit()


async def _try_consume_question(
    db: AsyncSession,
    org_id: int,
    day: dt.date,
    *,
    d_limit: int | None,
    m_limit: int | None,
) -> bool:
    """原子地"检查+扣减一次提问"：没超额则扣减成功返回 True，超额返回 False。

    为什么必须做成一条 SQL：
        原始写法是"查用量 → 在 Python 里比上限 → 再调 _bump 加一"。
        这三步之间没有任何锁，两个并发请求会同时看到旧用量、同时判定"没超"，
        结果就是双双放行 —— 额度上限形同虚设。
        把"上限判断"塞进 UPDATE 的 WHERE 里，由数据库在行锁内一次性完成
        "读当前值 + 比较 + 加一"，并发请求就会自然排队，只有真正没超的那个成功。

    日/月两个维度任一超额都不算成功。

    ⚠️ 月上限为什么不能直接塞进 UPDATE 的 WHERE：
        月用量是"本月所有用量行之和"（跨多行），单行 UPDATE 的 WHERE
        表达不了这个汇总条件。所以对月上限改用「先加锁、再判断」：
        先用 SELECT ... FOR UPDATE 锁住本月的用量行，在锁内算出月累计，
        不超才继续执行条件 UPDATE。
        · PostgreSQL：FOR UPDATE 真正生效，并发请求在此排队，不会同时放行
        · SQLite：FOR UPDATE 被忽略，但 SQLite 是单写者，本身串行

        （早期版本把 m_limit 收进来却没用，导致当天有用量行后月上限完全失效 ——
          并发下能无限突破月额度。这里补上。）
    """
    # 1) 月上限：先锁住本月用量行，再判断（防止并发同时通过）
    if m_limit is not None:
        locked = (
            await db.execute(
                select(UsageDaily.questions)
                .where(UsageDaily.org_id == org_id, UsageDaily.day >= month_start_local())
                .with_for_update()
            )
        ).scalars().all()
        if sum(locked) >= m_limit:
            await db.rollback()   # 释放锁
            return False

    # 2) 日上限：条件 UPDATE 一步完成"读+比较+加一"，并发安全
    values = {"questions": UsageDaily.questions + 1}
    conds = [UsageDaily.org_id == org_id, UsageDaily.day == day]
    if d_limit is not None:
        # 允许"已用量 < 上限"才加。注意用 < 而非 <=：
        # 上限 3 表示最多问 3 次，当前 2 时可以加（变成 3），当前 3 时不能加。
        conds.append(UsageDaily.questions < d_limit)

    res = await db.execute(
        update(UsageDaily).where(*conds).values(**values)
    )
    if res.rowcount > 0:
        await db.commit()
        return True

    # 没命中行有两种可能：
    #   ① 今天还没有用量行（第一次提问）→ 需要新建；
    #   ② 已超额 → 返回 False。
    existing = await get_daily_usage(db, org_id, day)
    if existing is None:
        # 月上限在上面已经检查并扣减过了，这里不再重复查（省一次往返）。
        # 但 d_limit == 0 这种"一次都不给"仍要在这里挡住：
        # 条件 UPDATE 命中不到行（0 < 0 不成立），会走到这个分支。
        if d_limit is not None and d_limit <= 0:
            return False
        try:
            db.add(UsageDaily(org_id=org_id, day=day, questions=1))
            await db.commit()
            return True
        except IntegrityError:
            # 并发下被别人抢先插入了：退回"条件累加"这条路径
            await db.rollback()
            res2 = await db.execute(
                update(UsageDaily).where(*conds).values(**values)
            )
            await db.commit()
            return res2.rowcount > 0
    return False


async def get_daily_usage(db: AsyncSession, org_id: int, day: dt.date | None = None) -> UsageDaily | None:
    """取某天（默认今天）的用量行。"""
    d = day or today_local()
    return await db.scalar(
        select(UsageDaily).where(UsageDaily.org_id == org_id, UsageDaily.day == d)
    )


async def get_month_usage(db: AsyncSession, org_id: int) -> dict[str, int]:
    """汇总本月（从 1 号到今天）的用量。"""
    start = month_start_local()
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(UsageDaily.questions), 0),
                func.coalesce(func.sum(UsageDaily.tokens_in), 0),
                func.coalesce(func.sum(UsageDaily.tokens_out), 0),
                func.coalesce(func.sum(UsageDaily.embedding_chunks), 0),
            ).where(UsageDaily.org_id == org_id, UsageDaily.day >= start)
        )
    ).one()
    return {
        "questions": int(row[0]),
        "tokens_in": int(row[1]),
        "tokens_out": int(row[2]),
        "embedding_chunks": int(row[3]),
    }


# ============================================================
# 额度判定
# ============================================================

def _over(used: int, limit: int | None) -> bool:
    """是否已经用完。注意 None=不限，0=一次都不给。"""
    return limit is not None and used >= limit


async def check_and_consume_question(db: AsyncSession, org_id: int | None) -> QuotaDecision:
    """问答前调用：判定能不能问，并且**先把这一次记上**。

    返回的 QuotaDecision.allowed=False 时，调用方应当拒绝这次请求。
    """
    # 平台超管（不隶属任何组织）不参与额度限制
    if org_id is None:
        return QuotaDecision(allowed=True)

    quota = await get_quota(db, org_id)

    # 组织被整体停服（欠费/到期）——哪怕没用完额度也不让用
    if quota is not None and not quota.is_service_enabled:
        return QuotaDecision(
            allowed=False,
            reason="该账号所属组织的服务已暂停，请联系管理员",
            status_code=402,
        )

    daily = await get_daily_usage(db, org_id)
    month = await get_month_usage(db, org_id)
    d_used = daily.questions if daily else 0
    m_used = month["questions"]

    d_limit = quota.daily_question_limit if quota else None
    m_limit = quota.monthly_question_limit if quota else None

    if _over(d_used, d_limit):
        return QuotaDecision(
            allowed=False,
            reason=f"今日提问次数已用完（{d_used}/{d_limit}），请明天再试或联系管理员提升额度",
            status_code=429,
            daily_used=d_used, daily_limit=d_limit,
            monthly_used=m_used, monthly_limit=m_limit,
        )
    if _over(m_used, m_limit):
        return QuotaDecision(
            allowed=False,
            reason=f"本月提问次数已用完（{m_used}/{m_limit}），请联系管理员提升额度",
            status_code=402,
            daily_used=d_used, daily_limit=d_limit,
            monthly_used=m_used, monthly_limit=m_limit,
        )
    # token 额度（如果配了）
    d_tok_limit = quota.daily_token_limit if quota else None
    m_tok_limit = quota.monthly_token_limit if quota else None
    d_tok = (daily.tokens_in + daily.tokens_out) if daily else 0
    m_tok = month["tokens_in"] + month["tokens_out"]
    if _over(d_tok, d_tok_limit):
        return QuotaDecision(
            allowed=False,
            reason="今日 token 用量已达上限，请明天再试或联系管理员提升额度",
            status_code=429,
            daily_used=d_used, daily_limit=d_limit,
            monthly_used=m_used, monthly_limit=m_limit,
        )
    if _over(m_tok, m_tok_limit):
        return QuotaDecision(
            allowed=False,
            reason="本月 token 用量已达上限，请联系管理员提升额度",
            status_code=402,
            daily_used=d_used, daily_limit=d_limit,
            monthly_used=m_used, monthly_limit=m_limit,
        )

    # 判定通过 → 原子扣一次（先扣后用，见文件头"取舍一"）。
    # ⚠️ 为什么不能只靠上面的判断：上面的"读用量 → 比较上限"不是原子的。
    # 两个并发请求可能同时读到 d_used = limit-1，都判定"没超"，于是都放行，
    # 实际用了 limit+1 次。真正的把关在下面这条带条件的 UPDATE 里：
    # 只有"当前用量仍低于上限"时它才会命中行，否则 rowcount=0 即视为超额。
    ok = await _try_consume_question(
        db, org_id, today_local(), d_limit=d_limit, m_limit=m_limit
    )
    if not ok:
        # 走到这里说明在"判断之后、扣减之前"被并发请求抢先用掉了额度。
        # 重新读一次真实用量，给出准确的提示文案。
        daily2 = await get_daily_usage(db, org_id)
        month2 = await get_month_usage(db, org_id)
        d_used2 = daily2.questions if daily2 else 0
        m_used2 = month2["questions"]
        if _over(d_used2, d_limit):
            return QuotaDecision(
                allowed=False,
                reason=f"今日提问次数已用完（{d_used2}/{d_limit}），请明天再试或联系管理员提升额度",
                status_code=429,
                daily_used=d_used2, daily_limit=d_limit,
                monthly_used=m_used2, monthly_limit=m_limit,
            )
        return QuotaDecision(
            allowed=False,
            reason=f"本月提问次数已用完（{m_used2}/{m_limit}），请联系管理员提升额度",
            status_code=402,
            daily_used=d_used2, daily_limit=d_limit,
            monthly_used=m_used2, monthly_limit=m_limit,
        )
    return QuotaDecision(
        allowed=True,
        daily_used=d_used + 1, daily_limit=d_limit,
        monthly_used=m_used + 1, monthly_limit=m_limit,
    )


async def add_llm_tokens(db: AsyncSession, org_id: int | None, tokens_in: int, tokens_out: int) -> None:
    """回答结束后记 token 用量（上游没返回用量时调用方传 0，这里自然什么都不做）。"""
    if org_id is None or (not tokens_in and not tokens_out):
        return
    await _bump(db, org_id, today_local(), tokens_in=tokens_in, tokens_out=tokens_out)


async def add_embedding_chunks(db: AsyncSession, org_id: int | None, chunks: int) -> None:
    """文档向量化完成后记用量（按文本块数）。"""
    if org_id is None or not chunks:
        return
    await _bump(db, org_id, today_local(), embedding_chunks=chunks)


async def get_summary(db: AsyncSession, org_id: int) -> dict:
    """给接口用的汇总：今日/本月用量 + 各自的上限 + 是否在服务中。"""
    quota = await get_quota(db, org_id)
    daily = await get_daily_usage(db, org_id)
    month = await get_month_usage(db, org_id)
    return {
        "org_id": org_id,
        "is_service_enabled": quota.is_service_enabled if quota else True,
        "daily": {
            "day": (daily.day if daily else today_local()),
            "questions": daily.questions if daily else 0,
            "tokens_in": daily.tokens_in if daily else 0,
            "tokens_out": daily.tokens_out if daily else 0,
            "embedding_chunks": daily.embedding_chunks if daily else 0,
            "question_limit": quota.daily_question_limit if quota else None,
            "token_limit": quota.daily_token_limit if quota else None,
        },
        "monthly": {
            "questions": month["questions"],
            "tokens_in": month["tokens_in"],
            "tokens_out": month["tokens_out"],
            "embedding_chunks": month["embedding_chunks"],
            "question_limit": quota.monthly_question_limit if quota else None,
            "token_limit": quota.monthly_token_limit if quota else None,
        },
    }
