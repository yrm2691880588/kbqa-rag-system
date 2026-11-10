"""密码强度策略单元测试（security.validate_password_strength）。

验证：短密码/纯数字/纯字母/常见弱密码/含用户名 一律拒绝；
合规密码（8位+字母数字）放行。

运行方式（backend 目录下）：
    python -m unittest tests.test_password_policy -v
"""
import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.security import validate_password_strength


class TestPasswordStrength(unittest.TestCase):
    """注册/改密共用的密码强度门槛。"""

    def assert_rejected(self, password: str, username: str = ""):
        ok, reason = validate_password_strength(password, username)
        self.assertFalse(ok, f"密码 {password!r} 应被拒绝，但通过了")
        self.assertTrue(reason, "拒绝时必须给出原因")

    def assert_accepted(self, password: str, username: str = ""):
        ok, reason = validate_password_strength(password, username)
        self.assertTrue(ok, f"密码 {password!r} 应通过，但被拒：{reason}")

    # ---- 长度 ----
    def test_too_short_rejected(self):
        for pw in ["1", "1234567", "A1b2c3"]:
            self.assert_rejected(pw)

    def test_min_length_accepted(self):
        self.assert_accepted("Ab123456")  # 恰好 8 位

    # ---- 内容构成 ----
    def test_pure_digits_rejected(self):
        for pw in ["11111111", "12345678", "88888888"]:
            self.assert_rejected(pw)

    def test_pure_letters_rejected(self):
        for pw in ["abcdefgh", "qwertyui", "aaaaaaaA"]:
            self.assert_rejected(pw)

    def test_mixed_accepted(self):
        # 用中性示例，避免把个人账号名写进公开仓库
        for pw in ["kbqa2026", "A1b2C3d4", "Test888888", "abc123XYZ"]:
            self.assert_accepted(pw)

    # ---- 常见弱密码黑名单 ----
    def test_common_weak_rejected(self):
        for pw in ["12345678", "password", "qwerty123", "abc12345", "admin123"]:
            self.assert_rejected(pw)

    # ---- 不能包含用户名 ----
    def test_contains_username_rejected(self):
        # 用户名 alice2024 出现在密码里 → 拒绝
        self.assert_rejected("alice202456", "alice2024")
        # 用户名够长(≥4)才参与校验
        self.assert_accepted("alice202456", "abc")  # 用户名只有 3 位，不参与

    def test_username_rule_uses_case_insensitive(self):
        self.assert_rejected("ALICE202456", "alice2024")

    # ---- 返回值规范 ----
    def test_reason_is_chinese_hint(self):
        ok, reason = validate_password_strength("12345678")
        self.assertFalse(ok)
        self.assertGreater(len(reason), 0)


if __name__ == "__main__":
    unittest.main()
