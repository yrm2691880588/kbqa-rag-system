"""知识库管理相关的请求/响应数据格式（Pydantic 模型）。"""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class KBCreate(BaseModel):
    """创建知识库：名称必填，简介可选。"""

    name: str = Field(min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(default=None, max_length=500, description="简介")


class KBUpdate(BaseModel):
    """更新知识库：可改简介与启用状态。"""

    description: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None


class KBOut(BaseModel):
    """知识库信息（列表/详情返回）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    is_active: bool = True
    doc_count: int = 0
    chunk_count: int = 0
    created_at: dt.datetime


class DocOut(BaseModel):
    """文档信息（含入库状态，前端进度条用）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kb_id: int
    filename: str
    doc_type: str
    file_size: int = 0
    status: str = "pending"
    progress: int = 0
    version: int = 1
    chunk_count: int = 0
    error_msg: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class ChunkItem(BaseModel):
    """单个知识片段（分块预览用）。"""

    chunk_index: int | None = None
    page: int | None = None
    text: str = ""


class EmbeddingHealth(BaseModel):
    """向量服务健康检查结果。"""

    ok: bool
    message: str


# ============================================================
# 批量上传的结果结构
# ============================================================


class BatchUploadFailOut(BaseModel):
    """批量上传时单个文件的失败原因。

    为什么要把失败原因单独列出来而不是拼成一句话：
        前端需要能逐条显示"哪个文件失败了、为什么"，
        拼成一句话就没法做结构化展示（也没法让用户点"重试这个"）。
    """

    filename: str
    reason: str


class BatchUploadResultOut(BaseModel):
    """批量上传的汇总结果（部分成功语义）。

    为什么 total 要单独给：
        前端可以据此显示"3 个成功 / 2 个失败（共 5 个）"，
        不用自己算。
    """

    total: int
    succeeded: int
    failed: int
    documents: list[DocOut] = []
    failures: list[BatchUploadFailOut] = []
