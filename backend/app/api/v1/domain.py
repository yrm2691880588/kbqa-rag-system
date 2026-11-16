"""领域配置接口：把当前领域的界面文案暴露给前端。

## 为什么需要这个接口

前端是编译产物，不能像后端那样读环境变量。
如果文案写死在前端组件里，换领域后界面上还写着原来的行业词——
用户会以为装错了。

所以：**前端不写死任何领域文案，一律从这里取。**

## 为什么不用构建时注入

另一种做法是 `vite --mode ecommerce` 在构建时把文案写进产物。
没有采用，因为：
  · 导出项目后改一次文案就要重新构建前端，无法只重启后端
  · 母项目要为每个领域准备一份 .env.<domain>，容易漏

运行时的代价是首屏有一瞬间的默认文案，用骨架屏可以掩盖。

## 权限

不需要登录：登录页本身就要显示领域名称（"XX知识库问答系统"）。
这些文案不含任何业务数据，公开无风险。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.domain import get_domain
from app.schemas.domain import DomainConfigOut

logger = logging.getLogger("kbqa.api.domain")

router = APIRouter(prefix="/domain", tags=["领域配置"])


@router.get("/config", response_model=DomainConfigOut)
async def domain_config() -> DomainConfigOut:
    """当前领域的界面文案（无需登录）。"""
    d = get_domain()
    return DomainConfigOut(
        name=d.name,
        display_name=d.display_name,
        app_title=d.app_title,
        login_slogan=d.login_slogan,
        # 标题留空时用展示名拼一个，保证前端总有东西可显示
        welcome_title=d.welcome_title or f"{d.display_name}知识库智能问答",
        welcome_hint=d.welcome_hint or "点击左侧「新对话」开始提问。",
        example_questions=list(d.example_questions),
    )
