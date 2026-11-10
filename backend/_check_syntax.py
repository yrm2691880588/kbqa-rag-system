"""写 Python 文件之后的「必过闸门」：让解释器自己编译一遍。

为什么需要它（真实教训）：
    本项目同一个低级错误犯了 5 次 —— 在中文说明里用了英文双引号，
    例如 print("说明"含引号的词"继续")，解析到中间就断了。
    每次都是「跑脚本 → 报一行错 → 改一处 → 再跑 → 又报下一处」，来回好几轮，
    白白浪费时间。

    所以定个规矩：**凡是我新写或改过的 .py 文件，执行前必须先过这一关。**

为什么不用"扫描引号"那种启发式判据：
    试过，不可靠。像 `"你好", "您好", "谢谢"` 这种一行里多个独立字符串，
    按引号成对配对的算法会把中间的中文当成"字符串里套引号"而误报
    （第一次扫描 112 个文件报了 454 处，全是误报）。
    **解释器自己的编译结果才是唯一权威**，所以这里就用它。

用法：
    python _check_syntax.py <文件或目录> [...]
退出码：0 = 全部通过；1 = 有文件编译不过
"""

import pathlib
import sys
import traceback


def check(path: pathlib.Path) -> tuple[bool, str]:
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return False, f"读取失败：{exc}"
    try:
        compile(source, str(path), "exec")
        return True, ""
    except SyntaxError as exc:
        line = (exc.text or "").rstrip()
        pointer = " " * max(0, (exc.offset or 1) - 1) + "^"
        detail = (
            f"第 {exc.lineno} 行：{exc.msg}\n"
            f"      {line}\n"
            f"      {pointer}"
        )
        # 这类错误 99% 是「中文说明里套了英文双引号」，直接把修法说出来
        if line.count('"') >= 4 and any(ord(c) > 127 for c in line):
            detail += "\n      💡 看起来是中文文本里套了英文双引号 —— 改成中文书名号「」即可"
        return False, detail
    except Exception as exc:      # 其他罕见编译期问题
        return False, f"编译异常：{exc.__class__.__name__}: {exc}"


def main(argv: list[str]) -> int:
    targets = [pathlib.Path(a) for a in argv[1:]]
    if not targets:
        print(__doc__)
        return 2

    files: list[pathlib.Path] = []
    for t in targets:
        if t.is_dir():
            files.extend(sorted(t.rglob("*.py")))
        elif t.exists():
            files.append(t)
        else:
            print(f"⚠️ 跳过不存在的路径：{t}")

    files = [f for f in files if "__pycache__" not in f.parts]
    failed: list[pathlib.Path] = []
    for f in files:
        good, detail = check(f)
        if not good:
            failed.append(f)
            print(f"❌ {f}\n      {detail}")

    print(f"\n检查 {len(files)} 个文件：通过 {len(files) - len(failed)} 个，失败 {len(failed)} 个。")
    if failed:
        print("失败的：" + "、".join(str(f) for f in failed))
        return 1
    print("全部通过 ✅ —— 可以放心执行。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        traceback.print_exc()
        sys.exit(3)
