"""对外 API Key 表：让企业把问答接进自家系统。

为什么需要这个：
    现在所有接口都要求"用户名 + 密码登录"，拿到的是会过期的登录令牌。
    企业要把它接进自己的客服系统/办公系统时，没法让程序去模拟登录——
    程序需要一个**长期有效、可随时吊销、且与具体员工无关**的凭证。这就是 API Key。

三个安全设计（都写在字段上）：
    ① **只存哈希**：数据库里没有明文 Key。数据库泄露也偷不走凭证。
       （代价：Key 只在创建那一瞬间显示一次，丢了只能重建。）
    ② **带前缀**：明文形如 kbqa_xxxxx，存库时另存前 8 位。
       好处是出问题时能在运维那边一眼认出"这是谁家的 Key"，
       而不用把整个 Key 存下来对比。
    ③ **归属到组织**：Key 是"某家客户"的，不是"某个人"的。
       所以员工离职不需要重建 Key，而客户欠费时可以一次吊销该组织的全部 Key。
"""

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 归属组织：所有用这个 Key 发起的请求，都按这个组织算额度、算隔离范围
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # 人给这个 Key 起的名字，例如"客服系统正式环境"
    name: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # 明文 Key 的 sha256（唯一的查询入口）。绝不存明文。
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    # 明文的前几位（形如 kbqa_ab12），仅用于展示与排查，不足以还原完整 Key
    key_prefix: Mapped[str] = mapped_column(String(16), default="", nullable=False)

    # 谁创建的（员工离职后能查出"这个 Key 是谁开的"）
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # 吊销开关：**不删行**，只打标记。因为"这个 Key 曾经存在过"本身是有用的记录，
    # 删掉就查不出来了（谁在什么时候吊销的、之前用过多少次都丢了）。
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
