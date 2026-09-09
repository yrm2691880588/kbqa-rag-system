"""健康检查：确认服务与数据库都活着。"""

from fastapi import APIRouter
from sqlalchemy import text

from app.core.deps import DbSession

router = APIRouter(prefix="/health", tags=["健康检查"])


@router.get("")
async def health(db: DbSession):
    """返回服务与数据库状态，部署/监控时轮询用。"""
    await db.execute(text("SELECT 1"))  # 探一下数据库连接
    return {"status": "ok", "database": "ok"}
