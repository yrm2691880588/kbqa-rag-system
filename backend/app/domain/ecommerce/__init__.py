"""电商领域包。

`spec` 是对外暴露的领域描述对象 —— Core 通过 `app.domain.load_domain("ecommerce")`
拿到它。没有这个对象的领域会在加载时报错（见 app/domain/__init__.py 的校验）。
"""

from app.domain.ecommerce.domain import spec

__all__ = ["spec"]
