"""登录失败记录表：用于账号锁定（防撞库）与 IP 维度的失败统计。

设计说明：
- 存在数据库而不是内存里 → 后端即使多进程/多台机器，统计结果也全局一致
- 登录成功时，会删除该用户名/IP 的历史失败记录（清零）
- 表会随时间自然增长，属低频写入，暂不需清理任务（单条仅几十字节）
"""

import datetime as dt

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LoginFailure(Base):
    __tablename__ = "login_failures"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 尝试登录的用户名（失败的才算）
    username: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    # 来源 IP（用于识别"同一批机器扫号"）
    ip: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
