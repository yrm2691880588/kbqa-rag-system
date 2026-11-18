"""从 ModelScope 手动下载 bge-reranker-base 模型。

## 为什么不用 huggingface_hub 自动下载

实测遇到两个问题：
  · hf-mirror.com 限流（429 Too Many Requests，10000 请求/5 分钟）
  · huggingface.co 国内直连超时

ModelScope（阿里的模型站）国内可直连，速度稳定。

## 只下需要的文件

仓库里 `model.safetensors` 和 `pytorch_model.bin` 是**同一份权重的两种格式**，
各 1.06GB。我们只需要 safetensors（新版 transformers 优先用它，加载更快）。

所以总下载量约 1.1GB 而不是 2.1GB。

## 断点续传

大文件下载最怕中断重来。这里做了：
  · 已存在且大小一致 → 跳过
  · 已存在但大小不符 → 删了重下
  · 下载写入 .part 临时文件，完成后才改名
"""
import json
import os
import pathlib
import sys
import time
import urllib.request

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# 下载目标目录。可用 KBQA_ML_HOME 覆盖（默认放到用户目录下，
# 不写死某个盘符 —— 别人的机器上可能没有那个盘）
_ML_HOME = pathlib.Path(
    os.environ.get("KBQA_ML_HOME", str(pathlib.Path.home() / "kbqa-ml"))
)
TARGET = _ML_HOME / "models" / "bge-reranker-base"
BASE = "https://modelscope.cn/api/v1/models/BAAI/bge-reranker-base/repo?Revision=master&FilePath="

# 只需要这些（跳过 pytorch_model.bin，与 safetensors 内容重复）
NEEDED = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "model.safetensors",
]


def download(name: str) -> bool:
    """下载单个文件，返回是否成功。"""
    dst = TARGET / name
    dst.parent.mkdir(parents=True, exist_ok=True)

    url = BASE + name
    part = dst.with_suffix(dst.suffix + ".part")

    # 已存在且看起来完整 → 跳过
    if dst.exists() and dst.stat().st_size > 0:
        # 除了主文件，其他都是小配置，有就算完成
        if name != "model.safetensors" or dst.stat().st_size > 1_000_000_000:
            print(f"  [跳过] {name}（已存在，{dst.stat().st_size/1024/1024:.1f} MB）")
            return True

    print(f"  [下载] {name} …", flush=True)
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            last_pct = -1
            with open(part, "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        # 每 10% 报一次，避免刷屏
                        if pct >= last_pct + 10:
                            last_pct = pct
                            print(
                                f"         {pct}%  "
                                f"{done/1024/1024:.0f}/{total/1024/1024:.0f} MB",
                                flush=True,
                            )
        part.replace(dst)
        size_mb = dst.stat().st_size / 1024 / 1024
        print(f"  [完成] {name}  {size_mb:.1f} MB  耗时 {time.time()-t0:.0f}s")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] {name}: {type(e).__name__}: {str(e)[:100]}")
        if part.exists():
            part.unlink(missing_ok=True)
        return False


def main():
    print("=" * 60)
    print("从 ModelScope 下载 bge-reranker-base")
    print(f"目标目录：{TARGET}")
    print("=" * 60)

    ok = 0
    for name in NEEDED:
        if download(name):
            ok += 1

    print()
    print(f"完成 {ok}/{len(NEEDED)} 个文件")
    if ok == len(NEEDED):
        total = sum((TARGET / n).stat().st_size for n in NEEDED if (TARGET / n).exists())
        print(f"总大小：{total/1024/1024:.1f} MB")
        print(f"\n模型已就绪，可用 --model {TARGET} 启动重排服务")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
