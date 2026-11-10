"""全局配置：从 backend/.env 读取，所有模块共用一份（缓存）。"""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 开发环境兜底密钥：仅在 APP_ENV != prod 时自动回填，保证本地开箱即跑。
# 生产环境绝不会使用它（生产下 SECRET_KEY 未配置会直接拒绝启动）。
DEV_FALLBACK_SECRET = "kbqa-dev-only-insecure-secret-key-change-me"


class Settings(BaseSettings):
    """应用配置项。字段名与 .env 中的 KEY 一一对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 运行环境：dev=本地开发（宽松自检）；prod=生产部署（严格自检，关键配置缺失直接拒绝启动）
    app_env: str = "dev"
    app_name: str = "KBQA-RAG"
    debug: bool = True
    api_prefix: str = "/api/v1"
    # 是否暴露接口文档（/docs）。生产可设为 false 减少信息暴露；演示场景保持 true
    expose_docs: bool = True

    # 数据库连接串（PostgreSQL 主方案 / SQLite 轻量降级方案）
    # 安全约定：源码不写死默认口令。必须由 backend/.env 的 DATABASE_URL 提供
    # （首次使用：复制 .env.example 为 .env 并填写。留空则后端启动连接数据库时会报错提示）
    database_url: str = ""

    # 安全：JWT 签名密钥与令牌有效期
    # 约定：源码不留可用默认值。dev 下留空会自动回填开发兜底密钥（本机可用）；
    #       prod 下必须显式配置强随机密钥（≥32 位），否则启动即失败（见 core/startup_check.py）
    secret_key: str = ""
    access_token_expire_minutes: int = 120
    refresh_token_expire_days: int = 7

    # 首次启动自动创建的管理员账号
    # 安全约定：账号名与密码都不设真实默认值，必须由 backend/.env 提供
    # （.env.example 中的占位值仅为示例；真实部署务必改为自己的强密码）
    admin_username: str = "admin"
    admin_password: str = ""

    # 注册是否必须提供组织邀请码。
    # False（默认）：不填邀请码的注册者会加入"默认组织" —— 适合"一家公司自己私有部署"的场景，
    #               同事自助注册即可，不用逐个发码。
    # True：不填（或填错）邀请码一律拒绝注册 —— 适合"一套系统服务多家客户"的 SaaS 场景，
    #       否则任何陌生人都能注册进默认组织，看到不该看的知识库。
    registration_requires_invite: bool = False

    # 允许跨域访问的前端地址（开发用 Vite）
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    log_level: str = "INFO"

    # ===== AI 服务配置（M2 知识库入库 / M3 问答 用）=====

    # --- 对话模型（M3 问答用）---
    # 默认 DeepSeek 官方（OpenAI 兼容协议）；v4-flash 为官方现行快速模型
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-v4-flash"
    llm_temperature: float = 0.3   # 低温度：回答更贴近资料原文，少发挥

    # --- 向量模型（M2 文档切块入库用）---
    # 供应商：阿里云百炼（DashScope），走 OpenAI 兼容接口
    # base_url 用通用域名（无需业务空间 WorkspaceId）；模型用通义 text-embedding-v4
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024        # v4 默认 1024 维
    embedding_batch_size: int = 16          # 每次同时向量化多少段文本（节省请求数）
    embedding_max_retries: int = 3          # 网络抖动时重试次数
    embedding_timeout: int = 60             # 单次请求超时（秒）

    # --- 重排模型（P2-21 检索精度优化）---
    # 重排是干什么的（大白话）：
    #   向量检索是"粗筛"——它把问题和资料都变成一串数字比距离，快，但不够准；
    #   重排是"精挑"——把粗筛出来的十几条交给一个专门的小模型，
    #   让它逐条判断"这条资料到底能不能回答这个问题"，再重新排序。
    #   效果上，问答的引用命中率通常会明显提升，代价是多一次外部调用（有少量费用）。
    #
    # 怎么开关：**填了 key 和 model 就启用，留空就自动跳过**（不需要额外的开关项，
    # 少一个开关就少一处"以为关了其实开着"的困惑）。
    rerank_api_key: str = ""
    rerank_model: str = ""              # 例：gte-rerank-v2（阿里云百炼）
    rerank_base_url: str = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    rerank_timeout_seconds: float = 10.0   # 重排超时（秒）。超时/出错一律"跳过重排、用原顺序"，绝不因此让问答失败

    # ===== M2 知识库入库相关 =====

    # Qdrant 向量库连接与集合名（所有知识库共用一个集合，按 kb_id 过滤）
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection: str = "kb_chunks"

    # 上传文件落盘目录（相对 backend 运行目录）
    upload_dir: str = "data/uploads"

    # ===== M3 问答检索参数 =====
    retrieval_top_k: int = 6            # 最终引用给模型的片段上限
    retrieval_candidate_k: int = 16     # 向量检索先取回的候选片段数（多召回，供过滤挑选）
    retrieval_score_threshold: float = 0.45  # 相似度绝对下限：低于该值视为"没命中"，不硬答
    retrieval_margin_ratio: float = 0.72  # 相对过滤：候选按"文档最高分"排序后，分数低于
    #                                  # 最高分文档的 ratio 倍的文档整篇丢弃（去噪关键参数）
    retrieval_max_docs: int = 3          # 回答最多引用几篇不同文档（避免无关文档凑数）
    retrieval_chunks_per_doc: int = 2    # 同一文档内最多取几条片段（防刷屏）
    llm_max_history_rounds: int = 8     # 多轮对话中最多携带几轮历史进上下文
    llm_max_tokens: int = 2048          # 单次回答最大输出长度
    # 说明：这里曾经有一个 llm_stream 开关（是否流式输出），已删除（P2-23 代码收口）。
    # 原因：网页端问答**只**支持流式（打字机效果是产品体验的一部分，关了就没意义）；
    # 需要"一次拿完整回答"的场景已经由对外接口 POST /open/chat 提供（非流式）。
    # 留着一个谁都不读的开关，只会让运维以为改了有用。
    llm_mock: bool = False              # 压测专用：True 时不调 DeepSeek，改发本地模拟流式回答
    #                                # （省测试费用；检索/去噪等真实逻辑不受影响，仅替换外部模型调用）
    # 流式请求里是否顺带索取 token 用量（用于额度计量）。
    # 部分兼容网关不认识这个参数，代码里已做兜底：拿不到就退回普通模式，不影响问答。
    llm_stream_usage: bool = True

    # ===== 登录安全 / 验证码 / 短信（账号体系加固用）=====
    # 短信供应商，三选一：
    #   mock    = 不真发短信，验证码固定 123456（仅本地开发与自动化测试用！）
    #   webhook = 通用 HTTP 短信网关，适配国内大多数短信代理商与企业自建网关
    #   aliyun  = 阿里云短信（官方接口，按文档实现，未用真实账号联调过）
    sms_provider: str = "mock"
    sms_code_ttl_minutes: int = 5    # 短信验证码有效期（分钟）
    sms_cooldown_seconds: int = 60   # 同一手机号重发间隔（防短信轰炸）
    sms_daily_limit: int = 10        # 同一手机号每天最多发几条
    sms_timeout_seconds: float = 8.0  # 调短信服务商的超时（秒）。发不出去要尽快失败，别拖着用户

    # --- webhook 通道（sms_provider=webhook 时必填 SMS_WEBHOOK_URL）---
    # URL 里可用占位符：{phone} 手机号、{code} 验证码、{text} 整条短信内容
    # 例：https://sms.example.com/api/send?apikey=xxx&mobile={phone}&content={text}
    sms_webhook_url: str = ""
    sms_webhook_method: str = "POST"   # GET / POST
    # POST 时的请求体模板（JSON 字符串，同样支持占位符）。
    # 留空则按表单字段 phone/code/text 发送 —— 这是最常见的约定。
    sms_webhook_body: str = ""
    # 额外请求头（JSON 字符串），例如 {"Authorization":"Bearer xxx"}。
    # 有些网关要求把密钥放在请求头里而不是 URL 里。
    sms_webhook_headers: str = ""

    # --- 阿里云短信通道（sms_provider=aliyun 时四项必填）---
    # 密钥只在服务端使用，**绝不要**写进前端或提交到 git
    sms_aliyun_access_key_id: str = ""
    sms_aliyun_access_key_secret: str = ""
    sms_aliyun_sign_name: str = ""      # 短信签名（需在阿里云控制台报备审核通过）
    sms_aliyun_template_code: str = ""  # 模板编号（模板里要有 ${code} 变量，且需审核通过）
    sms_aliyun_region: str = "cn-hangzhou"

    captcha_ttl_minutes: int = 5     # 图形验证码有效期（分钟）
    register_max_per_ip_hour: int = 3  # 同一 IP 一小时内最多注册几个账号（防批量刷号）
    login_fail_lock_count: int = 5     # 同一账号连续输错 N 次就临时锁定（防撞库）
    login_fail_lock_minutes: int = 15  # 账号锁定时长（分钟）
    login_ip_fail_limit: int = 20      # 同一 IP 15 分钟内累计输错 N 次也锁定（防脚本扫号）

    # ===== 内容安全审核（本地敏感词库）=====
    # 总开关。为什么默认开：内容违规的责任在平台，宁可误拦也不能裸奔。
    # 词表在 app/data/sensitive_words.txt，改完自动生效，无需重启。
    content_filter_enabled: bool = True
    # 用户提问命中敏感词时是否直接拦下。
    # 开着可以挡住"拿系统生成违规内容"的人；如果客户希望"只审回答、不审提问"，
    # 可以把它关掉（比如法律/医疗类知识库，提问里天然会出现这类词）。
    content_filter_block_question: bool = True
    # AI 回答命中敏感词时的处理方式：
    #   replace —— 掐断生成，替换成一句标准提示（推荐，保平台不担责）
    #   mask    —— 保留回答但把敏感片段打码（适合"想尽量答完"的场景）
    content_filter_answer_mode: str = "replace"
    # 自定义词表路径（留空则用内置的 app/data/sensitive_words.txt）
    content_filter_wordlist: str = ""

    # ===== 可调参数收口（P2-20）=====
    # 为什么要收口：以前这些数字直接写死在代码里，客户想"把上传上限调到 200MB"
    # 或者"把超时放宽一点"，都得改代码、重新打包、重新部署。
    # 收进配置之后，改一个环境变量、重启即可。
    #
    # ⚠️ 注意：这里只搬了"真正需要按客户/环境调"的那些；
    #    写死的算法常量（比如分块长度）不搬 —— 那是功能逻辑，不是配置。

    # 单个上传文件大小上限（MB）。
    # 调大的话要同步改 nginx 的 client_max_body_size（默认只有 1MB，前端会直接报错），
    # 并留意解析时占用的内存：一份 100MB 的 PDF 解析起来是很吃内存的。
    max_upload_mb: int = 100

    # 对话模型请求超时（秒）。流式回答总时长可能很长（长文档总结），给得比较宽；
    # 建连超时用来快速发现"网络不通/域名解析不了"，不需要那么宽。
    llm_request_timeout: float = 120.0
    llm_connect_timeout: float = 15.0

    # 健康探活单项超时（秒）。太小会在数据库偶尔卡顿时误报"不健康"，
    # 太大会让监控系统的探活请求本身超时（一般监控给 10 秒）。
    health_check_timeout: float = 5.0
    # 健康探活里"真实调用一次对话模型"那一步的超时（秒），比上面宽松些
    health_llm_probe_timeout: float = 15.0

    # ===== 数据库连接池（P2-17）=====
    # 大白话：连接池就是"预先备好几条到数据库的电话线，谁用谁占，用完立刻还"。
    # 下面这几个数决定"一个进程最多能同时拿几条线"，配错了会在高峰时突然连不上数据库
    # （而平时完全正常，属于最难查的一类故障）。
    db_pool_size: int = 5              # 常备连接数：闲着也留着的那些
    db_max_overflow: int = 10          # 临时可超借几条：高峰时不够用可以多借，之后自动还
    db_pool_timeout: float = 30.0      # 排队等连接的最长等待（秒）。超时会报"连接池等待超时"
    db_pool_recycle_seconds: int = 1800  # 连接用满 30 分钟就丢掉重开（防被防火墙/NAT 悄悄掐断）
    db_pool_pre_ping: bool = True      # 借出前先"试一下线还通不通"，不通就换一条（强烈建议保持开）
    # 生产环境实际跑几个进程（uvicorn 多核并行）。用于把"连接总量"算清楚：
    #     总占用上限 = 进程数 × (db_pool_size + db_max_overflow)
    # 这个数必须小于数据库的 max_connections，否则高峰期会连不上。
    web_concurrency: int = 1
    db_max_connections: int = 100      # 数据库端允许的最大连接数（PostgreSQL 默认 100）

    # ===== 数据清理与归档（P2-22）=====
    # 问答日志与聊天消息只保留这么多天，超出的会被清理脚本删除（删前先归档成文件）。
    # 为什么要有上限：这两类数据只会增加，一天 500 次问答一年就是 18 万条，
    # 时间长了数据库查询变慢、备份文件也越来越大。
    # 留多久由业务决定：多数企业客户希望"至少能查半年"，所以默认 180 天。
    log_retention_days: int = 180
    message_retention_days: int = 180
    # 清理前先把要删的内容导出到这个目录（一个文件一类数据，JSONL 格式）
    archive_dir: str = "data/archive"

    # ===== AI 调用治理（P2-16）=====
    # ① 短期限流：每个组织每分钟最多问几次（0 = 不限）。
    #    为什么要它：按天/按月的额度挡不住"某一分钟涌进 200 个请求"，
    #    超了会被大模型接口返回 429（用户看到的是报错）。这一层负责削平尖峰。
    ai_rate_limit_per_minute: int = 0

    # ② 熔断：外部服务连续失败到阈值就"跳闸"，接下来一段时间直接快速失败。
    #    为什么需要：外部接口坏了时，每个请求都干等 120 秒才失败，
    #    会让工作线程迅速被占满 —— "大模型挂了"变成"我们网站也打不开了"。
    ai_cb_enabled: bool = True
    ai_cb_fail_threshold: int = 5       # 连续失败几次跳闸
    ai_cb_open_seconds: float = 60.0    # 跳闸后多久内直接快速失败
    ai_cb_half_open_trials: int = 1     # 冷却结束后先放几个请求去试探

    # ③ 问题缓存：同样的问题直接返回上次的答案（省一次检索 + 一次模型调用）。
    #    默认**关闭**：答案与知识库内容有关，知识库刚更新时缓存会答"老话"，
    #    所以是否开启要由业务决定（内部知识库常年不变 → 开；资料天天改 → 别开或调小 TTL）。
    #    ⚠️ 缓存键里带有"组织与知识库范围"，不同客户绝不会共用缓存。
    qa_cache_enabled: bool = False
    qa_cache_ttl_seconds: int = 3600    # 缓存有效期（秒）
    qa_cache_max_entries: int = 500     # 最多缓存多少条（超出淘汰最久未用的）

    # ===== 文档解析队列（P2-15）=====
    # 由独立的工作进程从数据库任务表里领活干，好处是：进程重启任务不丢、
    # 多进程不会重复解析同一个文档、失败会自动重试。
    ingestion_worker_enabled: bool = True   # 总开关（想单独起工作进程时可关掉网页进程里的它）
    ingestion_poll_seconds: float = 2.0     # 没活干时多久查一次队列（太小会空转浪费 CPU）
    ingestion_max_attempts: int = 3         # 一个文档最多解析几次（防"文件本身坏了"无限重试烧钱）
    ingestion_stale_seconds: int = 900      # 超过这么久还卡在"处理中"，就认为领它的进程已经死了

    # ===== 日志（P2-19）=====
    # 日志格式：text = 人看的（本机开发用）；json = 日志系统看的（能被 ELK/Loki 直接检索）
    log_format: str = "text"
    # 说明：日志里会自动给每条加"请求编号"，并对密钥/手机号做脱敏（见 core/logging_setup.py）。
    #      这两件事都是强制的、不可配置关闭 —— 靠人自觉做不到，漏一处密钥就永久留在日志里了。

    @property
    def is_sqlite(self) -> bool:
        """判断当前是否 SQLite 模式（决定连接参数差异）。"""
        return self.database_url.startswith("sqlite")

    def pool_budget(self) -> dict:
        """算一算"最多会占用数据库多少条连接"（P2-17）。

        为什么要显式算这个数（大白话）：
            一个进程的连接池最多能拿 pool_size + max_overflow 条；
            生产环境通常跑多个进程（多核并行），所以总量还要乘进程数。
            这个数只要比数据库的 max_connections 大，就一定会在高峰期出问题 ——
            而且是"平时好好的、一忙就崩"这种最难查的故障。
            启动自检会把这个算术题的结果直接打给运维看。

        放在 Settings 上（而不是数据库模块里）的原因：
            它纯粹是**配置之间的算术**，不依赖任何数据库连接对象。
            这样启动自检在"数据库地址还没填"的时候也能安全地算出来，
            不会因为提前导入数据库模块反而先崩掉（那就盖住了真正要报的错）。
        """
        size = int(self.db_pool_size or 5)
        overflow = int(self.db_max_overflow or 0)
        workers = max(1, int(self.web_concurrency or 1))
        db_max = int(self.db_max_connections or 100)
        per_worker = size + overflow
        total = per_worker * workers
        return {
            "pool_size": size,
            "max_overflow": overflow,
            "per_worker": per_worker,
            "workers": workers,
            "total": total,
            "db_max_connections": db_max,
            "over_budget": total > db_max,
        }

    @property
    def is_prod(self) -> bool:
        """是否生产环境（决定是否启用严格启动自检）。"""
        return self.app_env in ("prod", "production")

    @model_validator(mode="after")
    def _normalize(self) -> "Settings":
        """规范化环境标识；非生产环境回填开发兜底密钥，保证本地开箱即跑。"""
        # 兼容 "PROD " / "Production" 等写法，避免判断失效
        self.app_env = (self.app_env or "dev").strip().lower()
        if self.app_env in ("production", "release"):
            self.app_env = "prod"
        if not self.is_prod and not self.secret_key.strip():
            self.secret_key = DEV_FALLBACK_SECRET
        return self


@lru_cache
def get_settings() -> Settings:
    """只读一次 .env，之后复用，避免频繁 IO。"""
    return Settings()


settings = get_settings()
