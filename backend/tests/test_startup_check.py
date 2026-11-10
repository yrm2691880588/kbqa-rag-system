"""启动自检测试：验证生产环境配置闸门与开发环境的宽松行为。

覆盖点：
  - 生产环境缺关键配置 → 判为致命错误（且 run_startup_checks 抛异常拒绝启动）
  - 生产环境配置完整   → 无致命错误
  - 开发环境           → 同样缺配置也不阻断（只提示）
  - 各类告警项（调试开关、弱管理员口令、本机地址、短信 mock）能被识别
"""

import unittest

from app.core.config import DEV_FALLBACK_SECRET, Settings
from app.core.startup_check import collect_startup_issues, run_startup_checks


def _make_settings(**overrides) -> Settings:
    """构造一个不读本地 .env 的配置对象，避免测试受本机环境影响。

    这里代表"一份**安全达标**的生产配置"——所以连多租户相关的注册准入开关也一并开启。
    如果哪天新增了一条告警而这里没跟着设，test_complete_prod_config_has_no_errors
    就会失败，正好起到"提醒同步更新基准配置"的作用。
    """
    base = dict(
        _env_file=None,
        app_env="prod",
        debug=False,
        secret_key="p" * 48,
        llm_api_key="sk-test-llm",
        embedding_api_key="sk-test-embed",
        database_url="postgresql+asyncpg://kbqa:pw@postgres:5432/kbqa",
        admin_username="admin",
        admin_password="Kbqa2026Prod",
        cors_origins=["https://kbqa.example.com"],
        sms_provider="aliyun",
        # 短信通道凭据：选了 aliyun 就必须给全，否则验证码发不出去（自检会提醒）
        sms_aliyun_access_key_id="test-ak-id",
        sms_aliyun_access_key_secret="test-ak-secret",
        sms_aliyun_sign_name="测试签名",
        sms_aliyun_template_code="SMS_TEST",
        qdrant_url="http://qdrant:6333",
        llm_mock=False,
        registration_requires_invite=True,
    )
    base.update(overrides)
    return Settings(**base)


class TestProdCriticalIssues(unittest.TestCase):
    """生产环境：致命项判定。"""

    def test_complete_prod_config_has_no_errors(self):
        """配置齐全的生产环境不应有致命错误。"""
        errors, warnings = collect_startup_issues(_make_settings())
        self.assertEqual(errors, [], f"不应有致命项，实际：{errors}")
        self.assertEqual(warnings, [], f"不应有告警，实际：{warnings}")

    def test_missing_secret_key_is_fatal(self):
        errors, _ = collect_startup_issues(_make_settings(secret_key=""))
        self.assertTrue(any("SECRET_KEY 未配置" in e for e in errors), errors)

    def test_placeholder_secret_key_is_fatal(self):
        errors, _ = collect_startup_issues(_make_settings(secret_key="dev-secret-change-me"))
        self.assertTrue(any("占位值" in e for e in errors), errors)

    def test_short_secret_key_is_fatal(self):
        errors, _ = collect_startup_issues(_make_settings(secret_key="abc12345"))
        self.assertTrue(any("长度不足" in e for e in errors), errors)

    def test_llm_mock_on_is_fatal(self):
        """压测开关误带上线必须被拦。"""
        errors, _ = collect_startup_issues(_make_settings(llm_mock=True))
        self.assertTrue(any("LLM_MOCK" in e for e in errors), errors)

    def test_missing_service_credentials_are_fatal(self):
        errors, _ = collect_startup_issues(
            _make_settings(llm_api_key="", embedding_api_key="", database_url="")
        )
        joined = " | ".join(errors)
        self.assertIn("LLM_API_KEY", joined)
        self.assertIn("EMBEDDING_API_KEY", joined)
        self.assertIn("DATABASE_URL", joined)

    def test_run_startup_checks_raises_on_prod(self):
        """生产环境有致命项时，启动检查必须抛异常（Fail Fast）。"""
        cfg = _make_settings(secret_key="", llm_mock=True)
        with self.assertRaises(RuntimeError) as ctx:
            run_startup_checks(cfg)
        self.assertIn("SECRET_KEY", str(ctx.exception))

    def test_run_startup_checks_disables_debug_in_prod(self):
        """生产环境下调试开关会被自动关闭（而不是只提示）。"""
        cfg = _make_settings(debug=True)
        run_startup_checks(cfg)
        self.assertFalse(cfg.debug)


class TestProdWarnings(unittest.TestCase):
    """生产环境：告警项判定（不阻断启动）。"""

    def test_weak_admin_password_warns(self):
        _, warnings = collect_startup_issues(_make_settings(admin_password="12345678"))
        self.assertTrue(any("ADMIN_PASSWORD" in w for w in warnings), warnings)

    def test_missing_admin_password_warns(self):
        _, warnings = collect_startup_issues(_make_settings(admin_password=""))
        self.assertTrue(any("ADMIN_PASSWORD 未设置" in w for w in warnings), warnings)

    def test_local_cors_warns(self):
        _, warnings = collect_startup_issues(
            _make_settings(cors_origins=["http://localhost:5173"])
        )
        self.assertTrue(any("CORS_ORIGINS" in w for w in warnings), warnings)

    def test_sms_mock_warns(self):
        _, warnings = collect_startup_issues(_make_settings(sms_provider="mock"))
        self.assertTrue(any("SMS_PROVIDER" in w for w in warnings), warnings)

    def test_local_qdrant_warns(self):
        _, warnings = collect_startup_issues(_make_settings(qdrant_url="http://127.0.0.1:6333"))
        self.assertTrue(any("QDRANT_URL" in w for w in warnings), warnings)

    def test_open_registration_warns_in_prod(self):
        """生产环境未强制邀请码注册 → 应当提醒（多租户串数据的风险）。"""
        _, warnings = collect_startup_issues(_make_settings(registration_requires_invite=False))
        self.assertTrue(
            any("REGISTRATION_REQUIRES_INVITE" in w for w in warnings), warnings
        )

    def test_invite_only_registration_does_not_warn(self):
        """反向对照：开了邀请码准入就不该再提醒（避免误报）。"""
        _, warnings = collect_startup_issues(_make_settings(registration_requires_invite=True))
        self.assertFalse(
            any("REGISTRATION_REQUIRES_INVITE" in w for w in warnings), warnings
        )


class TestDevEnvironment(unittest.TestCase):
    """开发环境：宽松，绝不阻断开箱即跑。"""

    def test_dev_missing_secret_is_not_fatal(self):
        errors, _ = collect_startup_issues(
            _make_settings(app_env="dev", secret_key="", admin_password="")
        )
        self.assertEqual(errors, [], f"开发环境不应有致命项，实际：{errors}")

    def test_dev_secret_is_backfilled(self):
        """开发环境留空密钥会自动回填兜底值，保证本机可用。"""
        cfg = _make_settings(app_env="dev", secret_key="")
        self.assertEqual(cfg.secret_key, DEV_FALLBACK_SECRET)

    def test_dev_llm_mock_allowed(self):
        """压测场景在开发环境是允许的。"""
        errors, _ = collect_startup_issues(
            _make_settings(app_env="dev", llm_mock=True, sms_provider="mock")
        )
        self.assertEqual(errors, [], f"开发环境不应拦压测开关，实际：{errors}")

    def test_run_startup_checks_does_not_raise_in_dev(self):
        cfg = _make_settings(app_env="dev", secret_key="", llm_mock=True)
        run_startup_checks(cfg)  # 不应抛异常


if __name__ == "__main__":
    unittest.main()
