"""问答接口（M3 核心）：POST /chat 以 SSE 流式返回回答，回答带引用片段。

流程：
1. 校验会话归属 → 读该会话历史 → 存下用户提问消息；
2. 首轮提问且会话仍叫"新对话"时，自动用问题前几字生成标题；
3. **开放性意图短路**：若问题被识别为"列举/总览"类（如"介绍全部资料""有哪些内容"），
   直接从数据库读知识库与文档清单主动列给用户，避免生硬的"未找到资料"；
4. 否则调用 rag.stream_answer 流式生成，逐事件转发给前端（meta/thinking/answer/no_hit/done/error）；
5. 流结束后把 assistant 回答（含引用快照）写入消息表，并记录问答日志（管理员洞察用）。

实现注意：流式生成期间**不能持有请求注入的数据库会话**（响应流结束后依赖才清理，
而生成器要在流中途写库），因此写库统一在生成器内部用独立会话完成。
"""

import asyncio
import datetime as dt
import json
import logging
import time

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.core.tenancy import resolve_kb_scope
from app.db.session import AsyncSessionLocal
from app.domain import get_domain
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.message import Message
from app.models.query_log import QueryLog
from app.models.session import ChatSession
from app.schemas.chat import ChatRequest
from app.services import ai_guard, content_filter, quota
from app.services.open_intent import detect_open_intent
from app.services.rag import stream_answer

logger = logging.getLogger("kbqa.api.chat")

# 后台任务的强引用集合。
#
# 为什么需要：asyncio 只 create_task 而不持有引用时，
# 任务对象可能在中途被垃圾回收，导致"保存中断回答"静默失败。
# 这是 asyncio 的已知陷阱，官方文档也明确要求保存引用。
_BG_TASKS: set = set()

router = APIRouter(prefix="/chat", tags=["问答"])

# 回答命中敏感词时，替换给用户看的标准提示语。
# 为什么用一句固定的话而不是"已删除违规内容"：固定话术不透露命中了什么，
# 也不给试探者任何反馈信号；同时它对正常用户是清楚的（知道该换个问法）。
_BLOCKED_MESSAGE = (
    "抱歉，这条回答涉及不适宜的内容，已被系统安全策略拦截。\n"
    "您可以换一种问法，或联系管理员确认知识库里的资料是否需要调整。"
)


def _safe_replacement(text_so_far: str) -> str:
    """回答命中敏感词后，决定该给用户显示什么。

    两种策略（由配置 content_filter_answer_mode 决定）：
      replace —— 整条换掉，只留一句标准提示。最保险，平台完全不担责；
      mask    —— 保留回答、只把敏感片段打码。尽量让用户拿到有用信息，
                 适合那些"敏感词只是知识库里恰好出现的普通词"的场景
                 （比如某行业术语被误收进词表）。
    """
    if (settings.content_filter_answer_mode or "replace").strip().lower() == "mask":
        return content_filter.mask(text_so_far)
    return _BLOCKED_MESSAGE


def _sse(event: str, data: dict) -> str:
    """把事件序列化成一行 SSE 文本（event + data 两行，空行结束）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

class _StreamSaver:
    """流式回答的增量保存器。

    ## 为什么需要独立会话

        调用方的 `async with AsyncSessionLocal() as adb` 在请求被取消时会一起失效。
        每次保存另开短会话，写完立刻关，互不影响。

    ## 为什么用 UPSERT 而不是每次 INSERT

        同一个回答要写很多次（边生成边写）。
        如果每次都 INSERT，库里会出现几十条不完整的消息。
        所以第一次写拿 id，后面都 UPDATE 那一条。
    """

    __slots__ = ("session_id", "citations", "message_id", "last_len", "_threshold")

    def __init__(self, session_id: int, citations: list | None, threshold: int = 120):
        self.session_id = session_id
        self.citations = citations or []
        self.message_id: int | None = None
        self.last_len = 0
        self._threshold = threshold

    def should_flush(self, text: str) -> bool:
        """累积到阈值才写，避免每个字都写一次数据库。"""
        return len(text) - self.last_len >= self._threshold

    async def flush(self, text: str, final: bool = False) -> None:
        """把当前内容写进库（新建或更新同一条）。

        Args:
            text: 目前累积的完整文本。
            final: 是否是最后一次写入（正常结束）。
        """
        import time as _time

        content = text.strip()
        if not content:
            return

        # 中断/未完成时加标注，让用户知道这不是完整答案
        if not final:
            content = content + chr(10) + chr(10) + "> ⏳ （回答生成中……）"

        try:
            async with AsyncSessionLocal() as db:
                if self.message_id is None:
                    m = Message(
                        session_id=self.session_id,
                        role="assistant",
                        content=content,
                        citations=self.citations or None,
                    )
                    db.add(m)
                    await db.flush()
                    self.message_id = m.id
                else:
                    m = await db.get(Message, self.message_id)
                    if m is None:
                        return
                    m.content = content
                    m.citations = self.citations or None
                    if final:
                        m.latency_ms = getattr(m, "latency_ms", None)

                sess = await db.get(ChatSession, self.session_id)
                if sess:
                    import datetime as _dt

                    sess.updated_at = _dt.datetime.now(_dt.timezone.utc)

                await db.commit()
            self.last_len = len(text)
        except Exception as exc:  # noqa: BLE001
            # 增量保存失败不应打断回答生成 —— 只是"这一版没存上"，
            # 下一批还会再试。
            logger.warning("增量保存失败（不影响回答）：%s", exc)

    async def finish(self, text: str) -> int | None:
        """正常结束时的最终写入。返回消息 id（供前端点赞用）。"""
        content = text.strip()
        if not content:
            return self.message_id

        try:
            async with AsyncSessionLocal() as db:
                if self.message_id is None:
                    m = Message(
                        session_id=self.session_id,
                        role="assistant",
                        content=content,
                        citations=self.citations or None,
                    )
                    db.add(m)
                    await db.flush()
                    self.message_id = m.id
                else:
                    m = await db.get(Message, self.message_id)
                    if m is not None:
                        m.content = content
                        m.citations = self.citations or None

                sess = await db.get(ChatSession, self.session_id)
                if sess:
                    import datetime as _dt

                    sess.updated_at = _dt.datetime.now(_dt.timezone.utc)

                await db.commit()
            return self.message_id
        except Exception as exc:  # noqa: BLE001
            logger.warning("最终保存失败：%s", exc)
            return self.message_id


async def _save_partial_answer(
    session_id: int,
    answer_buf: list[str],
    citations: list[dict] | None,
    started: float,
) -> None:
    """保存被中断的回答（已生成的部分）。

    Args:
        session_id: 会话 id。
        answer_buf: 已经累积的回答片段。
        citations: 已检索到的引用。
        started: 开始时间（算延迟用）。

    Note:
        这个函数**不抛异常**：保存失败只记日志。
        它是在"已经有异常"的路径上执行的，再抛一个只会掩盖原始问题。

    ⚠️ 必须**自己开一个独立会话**，不能用调用方传进来的那个。

        为什么（实测确认的坑）：
            这个函数在 `except CancelledError` 里执行，
            而调用方的 `async with AsyncSessionLocal() as adb` 已经开始退出，
            会话可能已被关闭 —— 往里 add 消息会**静默失败**。

            具体表现：answer_buf 有 2 块内容（日志可见），
            但一条消息都没落库，而且没有任何异常日志。
            排查了很久才发现是会话生命周期问题，不是 SQL 问题。

        独立会话是干净的，不受调用方事务状态影响。
    """
    import time as _time

    partial = "".join(answer_buf).strip()

    # 一点内容都没有 → 没必要存一条空消息
    if not partial:
        logger.info("中断时无已生成内容，不保存")
        return

    try:
        async with AsyncSessionLocal() as save_db:
            m = Message(
                session_id=session_id,
                role="assistant",
                content=partial
                + chr(10)
                + chr(10)
                + "> ⚠️ （回答被中断，以上是已生成的部分）",
                citations=citations or None,
                latency_ms=int((_time.time() - started) * 1000),
            )
            save_db.add(m)

            sess = await save_db.get(ChatSession, session_id)
            if sess:
                import datetime as _dt

                sess.updated_at = _dt.datetime.now(_dt.timezone.utc)

            await save_db.commit()
        logger.info("已保存中断的部分回答（%d 字）", len(partial))
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存中断回答失败：%s", exc)



@router.post("")
async def chat(payload: ChatRequest, current_user: CurrentUser, db: DbSession):
    """发起一轮知识库问答，SSE 流式返回（前端 fetch ReadableStream 消费）。"""
    # 0) 内容安全（第一道）：先审用户提问。
    #    为什么要审输入：不拦的话，这套系统就成了"用我们的算力生成违规内容"的工具，
    #    而且日志里留下的是客户自己的账号在发。
    if settings.content_filter_enabled and settings.content_filter_block_question:
        q_hit = content_filter.check(payload.question, mask=False)
        if q_hit.hit:
            logger.warning(
                "用户 %s 的提问命中敏感词，已拦截（分类：%s）",
                current_user.username, "、".join(q_hit.categories) or "未分类",
            )
            # 只告诉用户"命中哪一类"，**不回显具体是哪个词**：
            # 回显等于给出"试探词典"的反馈，容易被用来逐字试出词库内容。
            cats = "、".join(q_hit.categories) or "敏感"
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"您的问题包含不适宜的内容（涉及「{cats}」），请修改后重试",
            )

    # 1) 会话归属校验 + 读历史
    s = await db.get(ChatSession, payload.session_id)
    if s is None or s.user_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")

    hist_rows = (
        (await db.execute(
            select(Message).where(Message.session_id == s.id).order_by(Message.id)
        ))
        .scalars()
        .all()
    )

    question = payload.question
    if payload.regenerate:
        # 重新生成：找回"最后一条用户提问"，并从历史里**剔除**针对它的回答。
        #
        # 为什么必须剔除上一轮回答：
        #   如果把上一条 AI 回答也放进 history，模型倾向于复述它，
        #   结果只是"换个说法重复一遍"，不是真正的重新生成。
        last_user_idx = None
        for i in range(len(hist_rows) - 1, -1, -1):
            if hist_rows[i].role == "user":
                last_user_idx = i
                break
        if last_user_idx is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "这个会话还没有提问，无法重新生成")
        question = hist_rows[last_user_idx].content
        # 历史只到"这条提问之前"，不含它自己和它的回答
        hist_rows = hist_rows[:last_user_idx]

    history = [{"role": m.role, "content": m.content} for m in hist_rows]

    # 1.4) 短期限流：每分钟最多问几次。
    #      为什么在额度之前：额度是"按天/按月"的账，挡不住"某一分钟涌进来 200 个请求"；
    #      而外部大模型接口本身有速率限制，撞上去会返回 429（用户看到的是报错）。
    #      这一层负责削平尖峰。返回 429 的意思是"太频繁了"，与额度用完（402，要付费）区分开。
    rl_key = f"org:{current_user.org_id if current_user.org_id is not None else 'root'}"
    if not ai_guard.rate_limiter.allow(rl_key):
        wait = ai_guard.rate_limiter.retry_after_seconds(rl_key)
        logger.info("组织 %s 触发分钟级限流，需等待约 %d 秒", current_user.org_id, wait)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"提问太频繁了，请 {wait} 秒后再试（当前限制：每分钟 "
            f"{ai_guard.rate_limiter.per_minute} 次）",
        )

    # 1.5) 额度闸门：判定还能不能问，并把这一次**先记上**。
    #      放在这里（存消息之前）的原因：额度用完就不该在库里留下提问记录，
    #      否则问答历史会出现"有问没有答"的残条。
    #      先扣后用是为了防并发超额，详见 services/quota.py 文件头的说明。
    #
    # 重新生成不扣提问次数：它没有产生新的"提问"，只是重新回答同一个问题。
    # （token 用量仍然会计入 —— 那次模型调用是真的花了钱。）
    if not payload.regenerate:
        decision = await quota.check_and_consume_question(db, current_user.org_id)
        if not decision.allowed:
            logger.info("组织 %s 触发额度限制，已拒绝提问：%s", current_user.org_id, decision.reason)
            raise HTTPException(decision.status_code, decision.reason)

    # 2) 存用户消息；若首轮且未改名，自动取问题摘要做标题；刷新会话活跃时间
    #
    # ⚠️ 重新生成时**不能**再存一条用户消息、也不该再扣一次额度：
    #    用户点"重新生成"是对**同一个问题**再要一次回答，
    #    如果照常存消息，会话里会出现两条一模一样的提问 ——
    #    既让用户困惑，也把额度重复扣了一遍。
    if not payload.regenerate:
        db.add(Message(session_id=s.id, role="user", content=question))
        if s.title == "新对话":
            s.title = question[:18] + ("…" if len(question) > 18 else "")
    s.updated_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()

    kb_ids = await resolve_kb_scope(db, current_user, s.kb_scope)
    user_id = current_user.id
    session_id = s.id
    # 组织 id 提前取出来：事件生成器在响应流里执行，那时请求会话可能已关闭，
    # 再去读 ORM 对象的属性会失败，所以这里先落地成普通值
    org_id = current_user.org_id

    async def event_gen():
        """SSE 事件生成器：转发模型流 + 结束后独立会话写库。"""
        # 流结束后用于写库的字段
        answer_buf: list[str] = []
        # 增量保存器：每累积 120 字就落一次库。
        #
        # 为什么不能等结束才存（实测结论）：
        #   客户端断开时 Starlette 会取消整个请求任务，
        #   在 except 分支里 await 或 create_task 都会被一起取消。
        #   所以必须"边生成边存"，断开时库里已经有最新一版。
        stream_saver: _StreamSaver | None = None
        citations: list = []
        answered = False
        started = time.time()
        # 内容安全（第二道）：逐段检查 AI 回答，命中就立刻掐断生成。
        # 用 StreamGuard 而不是"等全部生成完再检查"：违规内容一旦吐到屏幕上就收不回来了，
        # 所以要在它还在往外冒的时候就截住。
        guard = content_filter.StreamGuard()
        blocked = False
        # token 用量：上游没返回时保持 0（额度里就只按"提问次数"算，不会误伤）
        tokens_in = 0
        tokens_out = 0

        async with AsyncSessionLocal() as adb:
            try:
                # 0) 开放性意图短路：用户问"介绍全部资料/有哪些内容"时，直接从 DB 列清单，
                #    避免生硬的"未找到资料"。
                if detect_open_intent(question):
                    text = await _build_guided_listing(adb, kb_ids, org_id)
                    # 这段文案是我们自己拼的（含知识库名、文档名），也过一遍审核：
                    # 客户万一给知识库起了个带敏感词的名字，同样不该原样吐出去
                    gf = content_filter.check(text, mask=False) if settings.content_filter_enabled else None
                    if gf is not None and gf.hit:
                        blocked = True
                        text = _BLOCKED_MESSAGE
                    answer_buf.append(text)
                    answered = not blocked
                    yield _sse("answer", {"text": text})
                    yield _sse("done", {
                        "latency_ms": int((time.time() - started) * 1000),
                        "citations": [],
                        "has_answer": answered,
                        "guided": not blocked,  # 标记由智能引导给出,前端可选用
                        "blocked": blocked,
                    })
                else:
                    # cache_scope 传组织 id：缓存键里带上"是谁在问"，
                    # 保证企业 A 问过的问题不会把答案发给企业 B。
                    async for event, data in stream_answer(
                        question, history, kb_ids, cache_scope=f"org:{org_id}"
                    ):
                        if event == "meta":
                            citations = data.get("citations", [])
                            stream_saver = _StreamSaver(session_id, citations)
                            yield _sse("meta", data)
                        elif event in ("thinking", "answer"):
                            if event == "answer":
                                piece = data.get("text", "")
                                # 逐段过闸：命中就停止生成，并把这轮回答整体替换掉
                                if settings.content_filter_enabled:
                                    hit = guard.feed(piece)
                                    if hit.hit:
                                        blocked = True
                                        logger.warning(
                                            "AI 回答命中敏感词，已掐断（分类：%s，用户：%s）",
                                            "、".join(hit.categories) or "未分类",
                                            user_id,
                                        )
                                        # 命中片段可能正好落在这一块里，所以把 piece 也算进去
                                        answer_buf = [_safe_replacement("".join(answer_buf) + piece)]
                                        yield _sse("blocked", {
                                            "text": answer_buf[0],
                                            "mode": settings.content_filter_answer_mode,
                                        })
                                        yield _sse("done", {
                                            "latency_ms": int((time.time() - started) * 1000),
                                            "citations": citations,
                                            "has_answer": False,
                                            "blocked": True,
                                        })
                                        break
                                answer_buf.append(piece)
                                # 增量落库：断开时最多丢 300 字
                                if stream_saver is None:
                                    stream_saver = _StreamSaver(session_id, citations)
                                cur_text = "".join(answer_buf)
                                if stream_saver.should_flush(cur_text):
                                    await stream_saver.flush(cur_text)
                            yield _sse(event, data)
                        elif event == "no_hit":
                            answered = False
                            # 未命中也要落一条 assistant 消息，让历史完整可续聊
                            answer_buf.append(data.get("message", "知识库中未找到相关资料。"))
                            yield _sse("no_hit", data)
                        elif event == "done":
                            answered = bool(data.get("has_answer"))
                            # 顺手把上游返回的 token 用量留下来，写库时一并记账
                            tokens_in = int(data.get("tokens_in", 0) or 0)
                            tokens_out = int(data.get("tokens_out", 0) or 0)
                            latency = data.get("latency_ms", 0)
                            yield _sse("done", data)
                        elif event == "error":
                            answer_buf.append(data.get("message", "生成失败，请重试。"))
                            yield _sse("error", data)
                            yield _sse("done", {"latency_ms": int((time.time() - started) * 1000), "citations": citations})

                # 3) 流结束：落 assistant 消息 + 刷新会话活跃时间 + 问答日志
                full_answer = "".join(answer_buf).strip()
                assistant_msg_id: int | None = None

                # 复用增量保存器做最终写入，而不是再 INSERT 一条。
                #
                # 为什么：生成过程中 _StreamSaver 已经写过几次同一条消息，
                # 这里如果重新 INSERT，库里会出现两条内容几乎一样的回答
                # （一条是"生成中"的半成品，一条是完整的），用户会困惑。
                #
                # finish() 的行为：
                #   · 增量写过 → UPDATE 那一条，去掉"生成中"标注
                #   · 从没写过（答案很短没到阈值）→ INSERT 一条
                if full_answer:
                    if stream_saver is None:
                        stream_saver = _StreamSaver(session_id, citations)
                    assistant_msg_id = await stream_saver.finish(full_answer)
                    # 记录延迟（这条是最终版，延迟才有意义）
                    try:
                        async with AsyncSessionLocal() as _db:
                            _m = await _db.get(Message, assistant_msg_id)
                            if _m is not None:
                                _m.latency_ms = int((time.time() - started) * 1000)
                                await _db.commit()
                    except Exception:  # noqa: BLE001
                        pass
                sess = await adb.get(ChatSession, session_id)
                if sess:
                    sess.updated_at = dt.datetime.now(dt.timezone.utc)
                adb.add(
                    QueryLog(
                        user_id=user_id,
                        session_id=session_id,
                        question=question,
                        kb_scope=kb_ids,
                        retrieval=citations or None,
                        answered=answered,
                        latency_ms=int((time.time() - started) * 1000),
                    )
                )
                await adb.commit()

                # 3.5) 记账：把这一轮的 token 用量累加进当天用量。
                #      放在提问次数之外单独记，是因为"次数"在提问那一刻已经扣过了，
                #      而 token 要等回答生成完才知道有多少。
                #      注意：真正的记账已移到外层 finally 统一兜底（见下方），
                #      这样客户端中途断开时也不会漏记。

                # 4) 把刚落库这条回答的编号告诉前端。
                #    为什么需要这一步：反馈接口要靠 message_id 定位"给哪条回答打分"，
                #    而这条回答是流式生成后由后端写入的，前端手里原本没有它的 id，
                #    结果就是"刚问完的这轮没法点赞"——恰恰是最该点赞的场景。
                #    放在 done 之后单独发一个事件，是为了不破坏既有前端的解析逻辑。
                if assistant_msg_id is not None:
                    yield _sse("saved", {"message_id": assistant_msg_id})
            except GeneratorExit:
                logger.warning(
                    "DEBUG 诊断：捕获 GeneratorExit，answer_buf 长度=%d，"
                    "内容前 40 字=%r",
                    len(answer_buf), "".join(answer_buf)[:40],
                )
                # 客户端主动断开（关页面/切会话/点停止生成）时，生成器被关闭会抛这个。
                # 不能当成错误处理，也不能向外 yield 任何东西（流已经没了）。
                logger.info("客户端已断开，停止生成（token 用量在 finally 里补记）")

                # ⚠️ 为什么用 create_task 而不是 await：
                #   GeneratorExit 里 **不能 await** —— 生成器正准备销毁，
                #   事件循环不允许它再挂起，await 会抛
                #   "RuntimeError: async generator ignored GeneratorExit"。
                #
                #   create_task 只是把协程排进事件循环，当前函数立刻返回，
                #   保存任务在后台独立跑完。
                #
                #   为什么不保存也无所谓？不行 —— 用户切回来只看到自己的提问，
                #   会以为回答被"吞"了（实测反馈过）。
                _bg = asyncio.create_task(
                    _save_partial_answer(session_id, answer_buf, citations, started)
                )
                # 持有引用防止被 GC 提前回收（asyncio 的已知陷阱：
                # 只 create_task 不保存引用，任务可能在中途被垃圾回收）
                _BG_TASKS.add(_bg)
                _bg.add_done_callback(_BG_TASKS.discard)
                raise
            except asyncio.CancelledError:
                logger.warning(
                    "DEBUG 诊断：捕获 CancelledError，answer_buf 长度=%d",
                    len(answer_buf),
                )
                # 同上，但这是"服务端取消任务"的路径（如超时、进程退出）。
                logger.info("问答流被取消（token 用量在 finally 里补记）")
                await _save_partial_answer(session_id, answer_buf, citations, started)
                raise
            except Exception as _exc:
                logger.warning(
                    "DEBUG 诊断：捕获 %s，answer_buf 长度=%d",
                    type(_exc).__name__, len(answer_buf),
                )
                logger.exception("问答流处理异常")
                yield _sse("error", {"message": "服务内部错误，请稍后重试"})
                yield _sse("done", {"latency_ms": int((time.time() - started) * 1000), "citations": []})
            finally:
                # 兜底记账：不管正常结束、异常、还是客户端中途断开，
                # 只要这一轮真实产生了 token 消耗，就必须记进额度。
                # 早期版本把记账放在 try 内部末尾，客户端一断开这段就被跳过 ——
                # 提问次数已扣、token 却没记，等于"按 token 限流"可以被断连绕过。
                try:
                    if tokens_in or tokens_out:
                        await quota.add_llm_tokens(adb, org_id, tokens_in, tokens_out)
                except Exception:
                    # 记账失败不能影响用户已经拿到的回答，记日志即可
                    logger.exception("补记 token 用量失败（org_id=%s）", org_id)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 让 nginx 类反代不缓冲流
        },
    )


async def _build_guided_listing(db, kb_ids: list[int] | None, org_id: int | None) -> str:
    """开放性问题引导：列出当前可用的知识库与各库内的 ready 文档名，供用户挑着问。

    Args:
        db: 写库用会话（在 event_gen 内由 AsyncSessionLocal 提供）。
        kb_ids: 已经过租户收敛的检索范围。
            ⚠️ 语义：None=不限制（仅平台超管）；**[]=该用户一个库都不能看**。
            两者必须分开处理 —— 早期版本这里写的是 `if kb_ids:`，
            空列表会退化成"不加过滤"，把别家组织的知识库也列了出来（跨租户泄露），已修正。
        org_id: 当前用户所属组织，用于兜底过滤（即使 kb_ids 为 None 也不会越界）。

    Returns:
        引导文案（中文多行），前端直接作为 AI 消息展示。
    """
    no_kb_text = (
        "目前系统里还没有可用的知识库。"
        "你可以请管理员先去「知识库管理」里创建一个，"
        "然后把资料上传进去，再回来提问效果会更好。"
    )

    # 明确无权访问任何库：直接给出提示，不查询、绝不退化成"查全部"
    if kb_ids is not None and len(kb_ids) == 0:
        return no_kb_text

    base_kb = select(KnowledgeBase).where(
        KnowledgeBase.is_active.is_(True),
        # 只列当前领域的库
        KnowledgeBase.domain == get_domain().name,
        # 租户隔离兜底：无论 kb_ids 传什么，都只在本组织范围内列
        KnowledgeBase.org_id == org_id,
    )
    if kb_ids is not None:
        base_kb = base_kb.where(KnowledgeBase.id.in_(kb_ids))
    kbs = (await db.execute(base_kb.order_by(KnowledgeBase.created_at.desc()))).scalars().all()

    if not kbs:
        return no_kb_text

    domain = get_domain()
    lines: list[str] = []
    # 示例问题必须来自领域配置：
    #   写死成某个行业的问题，换领域后会完全跑题。
    #   领域没配示例问题时不带举例。
    if domain.example_questions:
        examples = "」「".join(domain.example_questions)
        lines.append(
            "您好！您的问题比较宽泛，系统里没有能直接回答的单一文档。"
            "目前知识库里已收录以下内容，您可以挑一个具体的问题来问，例如：\n"
            f"  • 「{examples}」之类。\n"
        )
    else:
        lines.append(
            "您好！您的问题比较宽泛，系统里没有能直接回答的单一文档。"
            "目前知识库里已收录以下内容，您可以挑一个具体的问题来问。\n"
        )
    for kb in kbs:
        kb_title = f"【{kb.name}】"
        if kb.description:
            kb_title += f" — {kb.description}"
        lines.append(kb_title)
        doc_rows = (await db.execute(
            select(Document)
            .where(Document.kb_id == kb.id, Document.status == "ready")
            .order_by(Document.created_at.desc())
        )).scalars().all()
        if not doc_rows:
            lines.append("  · （暂无文档）")
        else:
            for d in doc_rows:
                lines.append(f"  · {d.filename}")
        lines.append("")
    lines.append("告诉我您想先了解哪款，我就帮您在知识库里找到答案并标上引用。")
    return "\n".join(lines)
