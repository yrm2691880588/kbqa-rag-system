"""组织额度表：限制每个客户组织能用多少（防 AI 成本裸奔，也是收费的依据）。

为什么额度挂在「组织」上而不是「人」上：
    我们卖给的是一家公司，签合同、开发票、算钱都是对公司。
    所以额度按组织设；组织管理员再自己决定怎么分给自己的人。

为什么用"每日 + 每月"两档而不是一档：
    只有月上限时，客户可以在最后一天把整月额度一口气刷完，服务直接停摆；
    只有日上限时，又挡不住"细水长流式"的长期滥用。
    两档一起用，才能既防突发也防累积。

字段留 None 表示**不限**（平台超管自己的组织、或者签了不限量合同的客户）。
"""

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OrgQuota(Base):
    __tablename__ = "org_quotas"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )

    # ---- 提问次数上限（最主要的成本闸门：每问一次都要调大模型）----
    daily_question_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_question_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ---- token 上限（更精细的成本口径；上游没返回用量时记 0，不会误伤）----
    daily_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 组织级总开关：欠费/合同到期/严重违规时整体停服。
    # 为什么和上面的额度分开：额度是"用到头了停"，这个是"立刻停"，
    # 运营需要一个不用改数字就能马上掐断的开关。
    is_service_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 备注：写清楚"为什么是这几个数字"，半年后自己回来看得懂
    note: Mapped[str] = mapped_column(String(255), default="", nullable=False)

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
