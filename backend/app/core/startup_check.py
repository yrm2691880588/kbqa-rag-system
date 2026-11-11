"""启动自检：防止开发/测试配置被带上生产环境。

设计原则：
  1. **Fail Fast**：生产环境（APP_ENV=prod）缺少关键配置时直接拒绝启动，
     而不是"带着隐患跑起来"——这类问题往往在被人利用时才暴露。
  2. **开发无感**：本地开发只打印提醒，绝不阻断开箱即跑。
  3. **可测试**：核心判定是纯函数（收配置 → 出问题清单），便于单元测试覆盖。

检查项一览（生产环境）：
  - SECRET_KEY 未配置 / 仍是占位值 / 长度不足  → 致命，拒绝启动
  - LLM_MOCK 开启（压测开关误带上线）          → 致命，拒绝启动
  - 大模型、向量模型密钥、数据库连接串缺失      → 致命，拒绝启动
  - DEBUG 开启、管理员弱口令、CORS 含本机地址、向量库指向本机、
    未开启邀请码准入、短信通道未接好              → 告警，记入日志
    （短信只告警的原因见 _check_sms_config 的说明：
      真正的安全由发送时 fail closed 兜底，不因为一个可选功能拦下整站启动）
"""

import logging

from app.core.config import Settings, settings
from app.core.security import validate_password_strength

logger = logging.getLogger("kbqa.startup")

# 视为"没改过的示例值"的密钥——出现即等同于未配置
PLACEHOLDER_SECRETS = {
    "dev-secret-change-me",
    "change-me",
    "changeme",
    "your-secret-key",
    "please-change-me",
    "secret",
    "123456",
}

# 生产密钥最短长度（太短可被暴力破解）
MIN_SECRET_LEN = 32

# 视为"本机地址"的特征串（生产环境出现即告警）
LOCAL_HOST_MARKS = ("localhost", "127.0.0.1", "0.0.0.0")


def collect_startup_issues(cfg: Settings) -> tuple[list[str], list[str]]:
    """收集配置问题，返回 (致命错误, 告警)。纯函数，便于单元测试。

    注意：开发环境下"致命错误"也只作为日志输出，不会中断启动（由调用方决定）。
    """
    errors: list[str] = []
    warnings: list[str] = []
    prod = cfg.is_prod

    # ---- 致命项（生产必须满足；开发环境同类问题降级为提醒）----

    # 1) JWT 签名密钥（开发环境留空会被自动回填，只有显式写成占位值时才会提醒）
    secret = (cfg.secret_key or "").strip()
    secret_issue = ""
    if not secret:
        secret_issue = "SECRET_KEY 未配置：生产环境必须显式设置强随机密钥（用于签发登录令牌）"
    elif secret.lower() in PLACEHOLDER_SECRETS:
        secret_issue = "SECRET_KEY 仍是示例/占位值：请生成强随机密钥后写入 backend/.env"
    elif len(secret) < MIN_SECRET_LEN:
        secret_issue = (
            f"SECRET_KEY 长度不足（当前 {len(secret)} 位，至少需 {MIN_SECRET_LEN} 位）：过短易被破解"
        )
    if secret_issue:
        (errors if prod else warnings).append(secret_issue)

    # 2) 压测开关误带上线（开启后不调用真实模型，用户会拿到假回答）——仅生产判定，
    #    开发环境开着它压测属正常用法
    if prod and cfg.llm_mock:
        errors.append("LLM_MOCK 处于开启状态：这是压测专用开关，生产环境必须关闭")

    # 3) 外部服务凭据
    if not (cfg.llm_api_key or "").strip():
        (errors if prod else warnings).append("LLM_API_KEY 未配置：问答功能无法调用对话模型")
    if not (cfg.embedding_api_key or "").strip():
        (errors if prod else warnings).append("EMBEDDING_API_KEY 未配置：文档入库与检索无法做向量化")

    # 4) 数据库连接
    if not (cfg.database_url or "").strip():
        (errors if prod else warnings).append("DATABASE_URL 未配置：后端无法连接数据库")

    # ---- 告警项（可运行但不理想）----

    # 5) 调试开关（生产会自动关闭，避免报错细节外泄）
    if prod and cfg.debug:
        warnings.append("DEBUG 处于开启状态：生产环境已自动关闭调试模式")

    # 6) 管理员口令
    admin_pw = (cfg.admin_password or "").strip()
    if not admin_pw:
        warnings.append("ADMIN_PASSWORD 未设置：首次启动不会自动创建管理员账号")
    else:
        ok, reason = validate_password_strength(admin_pw, cfg.admin_username)
        if not ok:
            warnings.append(
                f"ADMIN_PASSWORD 强度不足（{reason}）：管理员是最高权限账号，建议改为强密码"
            )

    # 7) 跨域来源（生产应填写真实域名）
    if prod and any(mark in origin for origin in cfg.cors_origins for mark in LOCAL_HOST_MARKS):
        warnings.append("CORS_ORIGINS 仍包含本机地址：生产环境应填写真实访问域名")

    # 8) 短信通道
    #
    # 注意：这一项只告警、不拒绝启动。原因是手机号登录只是**可选入口**
    # （用户名密码登录不受影响），而有些私有部署根本没有短信资质 ——
    # 因为一个用不到的功能让整站起不来是不划算的。
    # 真正的安全由"发送那一刻"强制兜底：生产环境用测试通道会**直接拒绝发送**，
    # 所以配错的后果是"验证码发不出去"，绝不是"所有人用 123456 登录别人账号"。
    _check_sms_config(cfg, prod, warnings)

    # 9) 向量库地址（容器部署时应使用服务名而非本机回环地址）
    if prod and any(mark in cfg.qdrant_url for mark in LOCAL_HOST_MARKS):
        warnings.append("QDRANT_URL 指向本机地址：容器部署时应填写服务名（例如 http://qdrant:6333）")

    # 10) 注册准入（多租户安全相关）
    # 若这套系统同时服务多家客户，却允许"不填邀请码即可注册"，那么任何陌生人都能
    # 注册进默认组织、看到该组织下的知识库。私有部署（只服务一家）时这是方便，
    # 对外 SaaS 时就是漏洞，所以生产环境给出明确提醒。
    if prod and not cfg.registration_requires_invite:
        warnings.append(
            "REGISTRATION_REQUIRES_INVITE 未开启：任何人注册都会加入默认组织。"
            "若本系统要同时服务多家客户，请开启该开关（注册必须凭组织邀请码）"
        )

    # 11) 数据库连接池够不够
    # 大白话：每个进程都会设一个"到数据库的电话线架子"，架子上的线数是有限的。
    # 生产环境通常跑好几个进程，所以"总用量 = 进程数 × 每个架子的线数"。
    # 只要这个总数超过数据库允许的上限，高峰期就会连不上数据库 ——
    # 而平时完全正常，属于最难查的一类故障。所以这里把这道算术题算出来直接给运维看。
    if prod:
        b = cfg.pool_budget()
        if b["over_budget"]:
            warnings.append(
                f"数据库连接可能不够用：{b['workers']} 个进程 × 每进程 {b['per_worker']} 条"
                f"（常备 {b['pool_size']} + 可超借 {b['max_overflow']}）= 最多 {b['total']} 条，"
                f"但数据库只允许 {b['db_max_connections']} 条。"
                "请调小 DB_POOL_SIZE / DB_MAX_OVERFLOW，或调大数据库的 max_connections"
            )
        else:
            logger.info(
                "[配置自检·生产] 数据库连接预算：%d 进程 × %d 条 = 最多 %d 条（数据库允许 %d 条），充足",
                b["workers"], b["per_worker"], b["total"], b["db_max_connections"],
            )

    return errors, warnings


def _check_sms_config(cfg: Settings, prod: bool, warnings: list[str]) -> None:
    """校验短信通道配置。

    ⚠️ 这里为什么只"告警"而**不拒绝启动**：
        手机号登录只是系统的一个可选入口（用户名密码登录完全不受影响）。
        有些私有部署根本没有短信资质、也用不到手机号登录 ——
        仅仅因为这一项没配好就让整站起不来，代价太大且没必要。

        真正的安全由**发送那一刻**强制兜底：生产环境一旦用的是测试通道，
        `services/sms.py` 里的 MockSmsSender 会直接拒绝发送（fail closed）。
        也就是说，配错了的后果是"验证码发不出去"（用户看到明确提示、日志里有 ERROR），
        而**绝不会**是"所有人用 123456 就能登录别人的账号"。
        一个"功能不可用"换一个"漏洞不存在"，这个交换是划算的。

        这里的作用是让运维**尽早看见**问题，而不是唯一的防线。
    """
    provider = (cfg.sms_provider or "").strip().lower()
    known = {"mock", "webhook", "aliyun"}
    prefix = "[配置自检]" if prod else "[配置提醒]"

    if provider not in known:
        warnings.append(
            f"{prefix} SMS_PROVIDER={provider or '(空)'} 不是已知的短信通道，"
            f"可选：{'、'.join(sorted(known))}。当前状态下发验证码会直接失败"
        )
        return

    if provider == "mock":
        if prod:
            warnings.append(
                f"{prefix} SMS_PROVIDER=mock：这是测试通道（验证码固定 123456）。"
                "生产环境已禁止它发送，因此**手机号登录/绑定将不可用**。"
                "需要该功能请改为 webhook 或 aliyun 并填好对应配置"
            )
        return

    if provider == "webhook":
        if not (cfg.sms_webhook_url or "").strip():
            warnings.append(f"{prefix} SMS_PROVIDER=webhook 但未配置 SMS_WEBHOOK_URL，验证码发不出去")
        elif "{phone}" not in cfg.sms_webhook_url and "{text}" not in cfg.sms_webhook_url:
            # 模板里既没有手机号也没有内容 —— 那这条短信发给谁、发什么都说不清
            warnings.append(
                f"{prefix} SMS_WEBHOOK_URL 里既没有 {{phone}} 也没有 {{text}} 占位符，"
                "短信服务商可能收不到收信人和内容"
            )
        return

    if provider == "aliyun":
        missing = [
            label for label, value in (
                ("SMS_ALIYUN_ACCESS_KEY_ID", cfg.sms_aliyun_access_key_id),
                ("SMS_ALIYUN_ACCESS_KEY_SECRET", cfg.sms_aliyun_access_key_secret),
                ("SMS_ALIYUN_SIGN_NAME", cfg.sms_aliyun_sign_name),
                ("SMS_ALIYUN_TEMPLATE_CODE", cfg.sms_aliyun_template_code),
            ) if not (value or "").strip()
        ]
        if missing:
            warnings.append(
                f"{prefix} SMS_PROVIDER=aliyun 但以下配置为空：" + "、".join(missing)
                + "（当前状态下验证码发不出去）"
            )


def run_startup_checks(cfg: Settings | None = None) -> None:
    """应用启动时调用：输出自检结果；生产环境存在致命问题时拒绝启动。

    Args:
        cfg: 待检查的配置（默认使用全局单例，测试时可注入）。
    Raises:
        RuntimeError: 生产环境存在致命配置问题。
    """
    cfg = cfg or settings
    errors, warnings = collect_startup_issues(cfg)
    mode = "生产" if cfg.is_prod else "开发"

    # 生产环境自动关闭调试模式（DEBUG=True 只在开发有意义）
    if cfg.is_prod and cfg.debug:
        cfg.debug = False

    for w in warnings:
        logger.warning("[配置自检·%s] %s", mode, w)

    if errors:
        for e in errors:
            logger.error("[配置自检·%s] %s", mode, e)
        if cfg.is_prod:
            raise RuntimeError(
                "生产环境配置自检未通过，已拒绝启动。请修正以下问题后重试：\n  - "
                + "\n  - ".join(errors)
            )
        logger.warning("[配置自检·开发] 以上问题在开发环境不阻断开机，上线前请务必处理")
    else:
        logger.info("[配置自检·%s] 通过（%d 条提醒）", mode, len(warnings))
