"""组织（租户）相关的接口数据结构。"""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class OrgCreate(BaseModel):
    """创建组织（客户）。"""

    name: str = Field(min_length=1, max_length=100, description="组织名称，例如某公司名")
    description: str | None = Field(default=None, max_length=500, description="备注")


class OrgUpdate(BaseModel):
    """修改组织：可改名、改备注、启停。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None


class OrgOut(BaseModel):
    """组织信息。

    ⚠️ 这里**故意不包含 invite_code**：
        邀请码是加入组织的凭证，等同于一把钥匙。如果任何能查组织的人都能看到它，
        隔离就形同虚设。所以只在"创建组织"与"主动重置邀请码"这两个动作里各返回一次。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    is_active: bool = True
    created_at: dt.datetime
    member_count: int = 0
    kb_count: int = 0

    # ---- 用量与额度（P1-B）：列表页一眼看出"哪家快用完了" ----
    # 这些字段由接口显式计算后填入；默认值保证老调用方不受影响。
    monthly_questions: int = 0                       # 本月已用提问次数
    monthly_question_limit: int | None = None        # 本月上限（None=不限）
    question_limit_per_day: int | None = None        # 每日上限（None=不限）
    is_service_enabled: bool = True                  # 组织级总开关


class OrgCreatedOut(BaseModel):
    """创建组织的返回：多带一个邀请码（仅此一次，请立即保存发给要加入的同事）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    is_active: bool = True
    created_at: dt.datetime
    invite_code: str


class InviteCodeOut(BaseModel):
    """重置后的新邀请码（仅此一次返回）。"""

    org_id: int
    invite_code: str
    message: str = "旧邀请码已立即失效，请把新邀请码发给要加入的同事"
