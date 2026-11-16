"""领域配置接口的数据结构。"""

from pydantic import BaseModel


class DomainConfigOut(BaseModel):
    """前端需要的领域文案。

    为什么单独开一个 schema 而不是直接把 DomainSpec 返回：
        DomainSpec 里有 forbidden、specific_hints 这类**内部配置**
        （合规禁令原文、意图词表），不该暴露给浏览器 ——
        禁令原文泄露出去，等于告诉别人"怎么绕过检查"。
        这里只挑界面真正需要展示的字段。
    """

    # 标识
    name: str
    display_name: str

    # 界面文案
    app_title: str
    login_slogan: str
    welcome_title: str
    welcome_hint: str
    example_questions: list[str] = []
