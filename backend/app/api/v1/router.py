"""v1 路由汇总：新增接口模块在这里登记即可自动挂载。"""

from fastapi import APIRouter

from app.api.v1 import (
    api_keys,
    auth,
    chat,
    documents,
    health,
    knowledge_bases,
    open_api,
    organizations,
    query_logs,
    quotas,
    sessions,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(organizations.router)
api_router.include_router(quotas.router)
api_router.include_router(query_logs.router)
api_router.include_router(api_keys.router)
api_router.include_router(open_api.router)
api_router.include_router(knowledge_bases.router)
api_router.include_router(documents.router)
api_router.include_router(sessions.router)
api_router.include_router(chat.router)
