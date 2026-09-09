"""验证码表：图形验证码 + 短信验证码 共用一张表。

kind 区分用途：
  - captcha：人机图形验证码（target 存随机 id，发给前端用于取图）
  - sms：短信验证码（target 存手机号）

所有验证码只存"答案的哈希"，即使数据库泄露也无法直接看到答案。
"""

import datetime as dt

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VerificationCode(Base):
    __tablename__ = "verification_codes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # captcha / sms
    kind: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    # 短信验证码：目标手机号
    target: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    # 发送方 IP（仅短信码用于防轰炸统计；图形码留空）
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 答案的 sha256 哈希（短信验证码是数字可直接哈希）
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
