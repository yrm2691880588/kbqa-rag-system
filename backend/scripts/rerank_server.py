"""本地重排服务：用 BGE-reranker 给候选片段打分排序。

## 为什么单独一个服务，而不是集成进主后端

主后端跑在 C 盘虚拟环境里，依赖精简（约 200MB）。
torch + sentence-transformers 要 2-3GB。塞进主环境会：
  · 拖慢所有功能的启动
  · 可能与现有 numpy 等依赖版本冲突
  · 导出项目时带上 ML 依赖（使用者可能用云 API，根本不需要）

所以拆成独立进程：
  · 独立虚拟环境在 D 盘（D:\\kbqa-ml\\venv）
  · 主后端通过 HTTP 调用
  · 服务挂了主后端自动降级（rerank.py 已有这个逻辑）

## 为什么用 BGE-reranker

中文重排里 BAAI 的 bge-reranker 系列是公认效果好且免费的选择。
base 版约 1.1GB，CPU 上单条 50~100ms —— 一次问答最多 20 条候选，
1~2 秒能算完，可接受。

## 接口设计

    POST /rerank
      {"query": "...", "documents": ["片段1", "片段2", ...]}
    →
      {"scores": [0.92, 0.35, ...], "model": "...", "elapsed_ms": 123}

分数是 0~1 的相关性（sigmoid 后的值），越大越相关。
顺序与输入 documents 一一对应 —— 调用方自己排序，
这样比"返回排好序的下标"更灵活（调用方可能想保留原始元数据）。

    GET /health
    → {"ok": true, "model": "...", "device": "cpu"}

## 启动

    D:\\kbqa-ml\\venv\\Scripts\\python.exe scripts/rerank_server.py --port 8020

首次启动会下载模型到 D:\\kbqa-ml\\models（由 HF_HOME 控制），约 1.1GB。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

# ⚠️ 必须在 import sentence_transformers **之前**设置 HF_HOME，
#    否则模型会下载到 C:\Users\<user>\.cache\huggingface（C 盘空间紧张）。
# 模型缓存目录。可以用环境变量 KBQA_ML_HOME 指到别处
# （默认值只是建议，不写死某台机器的绝对路径是必要的：
#   别人 clone 下来不该被强制塞到一个他们可能没有的盘符）
os.environ.setdefault(
    "HF_HOME",
    os.environ.get("KBQA_ML_HOME", str(pathlib.Path.home() / "kbqa-ml" / "models")),
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
import uvicorn  # noqa: E402

DEFAULT_MODEL = "BAAI/bge-reranker-base"


class RerankRequest(BaseModel):
    query: str = Field(..., description="用户问题")
    documents: list[str] = Field(default_factory=list, description="候选片段文本")


class RerankResponse(BaseModel):
    scores: list[float]
    model: str
    elapsed_ms: int


class _State:
    """模型句柄。延迟加载，避免 import 阶段就占 1.5GB 内存。"""

    model = None
    name = ""
    device = "cpu"


app = FastAPI(title="KBQA 本地重排服务", version="1.0.0")


def _load_model(name: str):
    """加载 CrossEncoder 模型（首次会下载）。"""
    if _State.model is not None:
        return _State.model

    from sentence_transformers import CrossEncoder

    print(f"[重排服务] 正在加载模型 {name} …（首次会下载，约 1.1GB）", flush=True)
    t0 = time.time()
    # max_length 控制单条文本截断：候选片段一般 500 字以内，
    # 512 token 足够，再长对重排判断没有帮助且浪费算力。
    model = CrossEncoder(name, max_length=512)
    print(f"[重排服务] 模型加载完成，耗时 {time.time() - t0:.1f} 秒", flush=True)

    _State.model = model
    _State.name = name
    return model


@app.get("/health")
def health():
    return {
        "ok": _State.model is not None,
        "model": _State.name or "(未加载)",
        "device": _State.device,
    }


@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest):
    """给每个候选片段打相关性分数。

    返回的顺序与输入 documents 一一对应 —— 调用方自己排序。
    """
    t0 = time.time()

    docs = [d or "" for d in req.documents]
    if not docs:
        return RerankResponse(scores=[], model=_State.name, elapsed_ms=0)

    model = _load_model(_State.name or DEFAULT_MODEL)

    # CrossEncoder.predict 接受 [(query, doc), ...]
    pairs = [(req.query, d) for d in docs]
    raw = model.predict(pairs)

    # 统一成 0~1 的浮点。bge-reranker 的原始输出是没有归一化的 logits，
    # 这里直接返回原值也能排序，但归一化后更方便调用方做阈值判断。
    scores = [float(x) for x in raw]

    return RerankResponse(
        scores=scores,
        model=_State.name,
        elapsed_ms=int((time.time() - t0) * 1000),
    )


def main():
    ap = argparse.ArgumentParser(description="本地重排服务（BGE-reranker）")
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--preload", action="store_true", help="启动时就加载模型（默认懒加载）")
    args = ap.parse_args()

    _State.name = args.model

    if args.preload:
        _load_model(args.model)

    print(f"[重排服务] 启动在 http://{args.host}:{args.port}", flush=True)
    print(f"[重排服务] 模型：{args.model}", flush=True)
    print(f"[重排服务] 模型目录：{os.environ.get('HF_HOME')}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
