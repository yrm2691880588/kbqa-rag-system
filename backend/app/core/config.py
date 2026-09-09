"""全局配置：从 backend/.env 读取，所有模块共用一份（缓存）。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置项。字段名与 .env 中的 KEY 一一对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "KBQA-RAG"
    debug: bool = True
    api_prefix: str = "/api/v1"

    # 数据库连接串（PostgreSQL 主方案 / SQLite 轻量降级方案）
    # 安全约定：源码不写死默认口令。必须由 backend/.env 的 DATABASE_URL 提供
    # （首次使用：复制 .env.example 为 .env 并填写。留空则后端启动连接数据库时会报错提示）
    database_url: str = ""

    # 安全：JWT 签名密钥与令牌有效期
    secret_key: str = "dev-secret-change-me"
    access_token_expire_minutes: int = 120
    refresh_token_expire_days: int = 7

    # 首次启动自动创建的管理员账号
    # 安全约定：密码不设默认值，必须由 backend/.env 的 ADMIN_PASSWORD 提供
    # （.env.example 中的 123456 仅为示例占位；真实部署务必改为强密码）
    admin_username: str = "pjs"
    admin_password: str = ""

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

    # --- 重排模型（M4 检索精度优化用，预留）---
    rerank_api_key: str = ""
    rerank_model: str = ""

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
    llm_stream: bool = True             # 默认流式输出
    llm_mock: bool = False              # 压测专用：True 时不调 DeepSeek，改发本地模拟流式回答
    #                                # （省测试费用；检索/去噪等真实逻辑不受影响，仅替换外部模型调用）

    @property
    def is_sqlite(self) -> bool:
        """判断当前是否 SQLite 模式（决定连接参数差异）。"""
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    """只读一次 .env，之后复用，避免频繁 IO。"""
    return Settings()


settings = get_settings()
