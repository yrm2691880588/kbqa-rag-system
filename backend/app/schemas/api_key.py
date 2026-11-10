"""对外 API 与 API Key 的接口数据结构（P1-G）。"""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class ApiKeyCreate(BaseModel):
    """创建 API Key。

    org_id 只有平台超管才需要填（给某个客户开 Key）；
    组织管理员不填，默认就是自己所在的组织。
    """

    name: str = Field(default="", max_length=64, description="给这个 Key 起个名，例如「客服系统」")
    org_id: int | None = Field(default=None, description="平台超管可指定组织；组织管理员忽略")
    expires_days: int | None = Field(
        default=None, ge=1, le=3650,
        description="多少天后过期；不填=永不过期（建议填，便于定期轮换）",
    )


class ApiKeyOut(BaseModel):
    """API Key 的列表项 —— **不含明文**（明文只在创建时返回一次）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    name: str
    key_prefix: str
    revoked: bool
    created_by: int | None = None
    expires_at: dt.datetime | None = None
    last_used_at: dt.datetime | None = None
    use_count: int = 0
    created_at: dt.datetime


class ApiKeyCreatedOut(ApiKeyOut):
    """创建时的返回：**多带一个明文 key**，仅此一次。

    为什么只给一次：数据库里只存哈希，服务器自己也还原不出明文。
    丢了就只能吊销重建（这也是设计意图：明文不落盘，泄露面最小）。
    """

    key: str
    message: str = "请立刻复制保存。这个 Key 只显示这一次，关掉后就查不到了。"


# ============ 对外问答接口 ============


class OpenChatRequest(BaseModel):
    """企业系统调用问答接口的请求体。"""

    question: str = Field(min_length=1, max_length=2000, description="用户问题")
    kb_ids: list[int] | None = Field(
        default=None,
        description="限定检索哪些知识库；不填=该组织全部可用知识库",
    )
    history: list[dict] | None = Field(
        default=None,
        description="可选的多轮上下文，形如 [{\"role\":\"user\",\"content\":\"...\"}]",
    )


class OpenCitation(BaseModel):
    """引用来源。"""

    index: int = 0
    doc_name: str = ""
    page: int | None = None
    text: str = ""
    score: float = 0.0
    kb_id: int | None = None


class OpenChatResponse(BaseModel):
    """对外问答的返回。

    设计成"一次给全"（非流式）：
        企业系统对接多半是后端到后端的调用，拿完整 JSON 更好处理；
        流式（打字机效果）是给人看的界面才需要。
    """

    answer: str
    citations: list[OpenCitation] = Field(default_factory=list)
    has_answer: bool = False
    blocked: bool = False           # 是否被内容安全策略处理过
    latency_ms: int = 0
    remaining_today: int | None = None   # 今日剩余提问次数（不限量时为 null）
