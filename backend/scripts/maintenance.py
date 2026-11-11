"""数据清理与归档。

怎么用（backend 目录下）：

    # 第一步：先看看会删什么（**默认只预览，一条都不删**）
    python scripts\\maintenance.py

    # 第二步：确认没问题，再真删（会先把要删的内容导出成文件）
    python scripts\\maintenance.py --apply

    # 想连归档都不做（不推荐，除非你另有备份）
    python scripts\\maintenance.py --apply --no-archive

    # 只清某一类
    python scripts\\maintenance.py --apply --only 问答日志

⚠️ 为什么默认不删：
    "本想先看看结果，结果手一抖就删了"是这类工具最经典的翻车方式。
    所以这里把"预览"和"执行"分成两个明确不同的命令，必须显式加 --apply 才会动手。

⚠️ 为什么先归档再删：
    万一发现删错了（比如业务上其实还要查三年前的问答记录），导出的文件还在，
    可以重新导入回去。归档文件是一个文件一类数据，JSONL 格式，用记事本或 Excel 都能打开。

建议的执行方式：
    放到服务器定时任务里，每天凌晨跑一次：
      · Linux：crontab 加一行  `0 4 * * * cd /path/to/backend && python scripts/maintenance.py --apply >> /var/log/kbqa-maintenance.log 2>&1`
      · Windows：任务计划程序里建一个每天 4:00 执行的任务
    先让它跑一周"只看不删"，观察日志里的条数是否合理，再改成 --apply。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.db.session import AsyncSessionLocal  # noqa: E402
from app.services import maintenance  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="数据清理与归档")
    parser.add_argument("--apply", action="store_true",
                        help="真的执行删除。不加这个参数时只预览，一条都不会删")
    parser.add_argument("--no-archive", action="store_true",
                        help="删除前不做归档（不推荐：删了就真没了）")
    parser.add_argument("--only", default="", help="只处理某一类（按报告里的类别名）")
    args = parser.parse_args()

    async with AsyncSessionLocal() as db:
        report = await maintenance.run_cleanup(
            db,
            apply=args.apply,
            archive=not args.no_archive,
        )

    mode = "已执行删除" if report.applied else "预览（未删除任何数据）"
    print("=" * 78)
    print(f"数据清理：{mode}")
    print("=" * 78)
    print(f"{'类别':<16}{'条数':>10}  {'说明'}")
    print("-" * 78)
    for p in report.plans:
        if args.only and p.name != args.only:
            continue
        note = p.note or p.description
        print(f"{p.name:<16}{p.count:>10}  {note}")

    if not report.applied:
        print("-" * 78)
        print(f"合计可清理：{report.total_planned} 条")
        print("\n要真的执行，请在命令后加 --apply（会先把内容归档成文件）")
        return 0

    print("-" * 78)
    print(f"实际删除：{report.total_deleted} 条")
    if report.archived_files:
        print("\n归档文件（删掉的内容都存在这里，可随时查回）：")
        for f in report.archived_files:
            print(f"  · {f}")
    if report.errors:
        print("\n⚠️ 以下类别出错（其余类别不受影响）：")
        for e in report.errors:
            print(f"  · {e}")
        return 1

    print("\n提示：用量统计（计费依据）永远不会被清理，只报告不删除。")
    return 0


if __name__ == "__main__":
    print("提示：本命令**默认只预览、不删除**；要真删请加 --apply。\n")
    sys.exit(asyncio.run(main()))
