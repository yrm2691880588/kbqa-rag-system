"""数据表模型汇总：import 本模块即可让所有表注册到 Base.metadata。"""

from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.login_failure import LoginFailure
from app.models.message import Message
from app.models.query_log import QueryLog
from app.models.refresh_token import RefreshToken
from app.models.session import ChatSession
from app.models.user import User
from app.models.verification_code import VerificationCode

__all__ = [
    "User",
    "RefreshToken",
    "ChatSession",
    "Message",
    "KnowledgeBase",
    "Document",
    "QueryLog",
    "VerificationCode",
    "LoginFailure",
]
