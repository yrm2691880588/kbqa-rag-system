"""SQLAlchemy 声明式基类：所有数据表模型都继承它。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """统一基类，后续建表/迁移都基于它。"""
