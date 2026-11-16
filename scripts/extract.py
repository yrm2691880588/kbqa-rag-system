#!/usr/bin/env python
"""从母项目裁剪出一个只含单一领域的独立项目。

## 它解决什么问题

母项目（本仓库）同时装着多个领域：电商、财经、通用。
但有时你需要一个**只属于某个领域**的项目 —— 交给别人二次开发、
或者自己单独维护一条产品线。

本脚本把「Core + 指定的一个领域」复制出去，并做三件事：

  1. **物理隔离**：产物里不含其他领域的任何代码、词表、文案、资料
  2. **结构拍平**：把 `app/domain/<name>/` 的内容收进 `app/domain/`，
     让产物看起来就是一个原生单领域项目，而不是"母项目的切片"
  3. **移除多领域机制**：`KNOWN_DOMAINS` 只留自己，`DOMAIN` 固定写死，
     用户不需要理解"还能切换领域"这回事

## 用法

    # 导出电商到仓库外的同级目录
    python scripts/extract.py ecommerce ../kbqa-ecommerce

    # 指定输出位置
    python scripts/extract.py finance D:/projects/kbqa-finance

    # 只看会复制什么，不真的写文件
    python scripts/extract.py ecommerce ../kbqa-ecommerce --dry-run

## 导出后是分叉

产物与母项目**不再关联**。母项目后续修 Core 的 bug 不会自动流入 ——
这是刻意的，否则会覆盖用户在产物上的改动。详见 docs/adr/0002。
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import re
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径基准
# ---------------------------------------------------------------------------
# 本文件在 <母项目>/scripts/extract.py，所以上两级就是母项目根目录。
# 用相对定位而不是写死绝对路径：母项目被移动/改名后依然能用。
REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"

# ---------------------------------------------------------------------------
# 复制规则
# ---------------------------------------------------------------------------

# 顶层条目：原样带过去
TOP_LEVEL_FILES = [
    "LICENSE",
    "docker-compose.yml",
    ".gitignore",
    ".env.example",
    "启动.bat",
    "停止.bat",
]

# 顶层目录：原样带过去
TOP_LEVEL_DIRS = [
    ".github",
    "frontend",
]

# 后端要带的内容（不含 data/、__pycache__ 等运行时产物）
BACKEND_ENTRIES = [
    "app",
    "migrations",
    "tests",
    "scripts",
    "alembic.ini",
    "requirements.txt",
    "Dockerfile",
    "run_tests.py",
]

# 导出产物里不该出现的文件（相对 backend/ 的路径）
#
#   scripts/extract.py          母项目的导出工具，产物里留着会让人困惑
#   tests/test_domain_isolation.py
#       这是在**多领域**前提下才有意义的测试：它遍历 KNOWN_DOMAINS
#       检查各领域互不污染。导出项目只有一个领域，这个测试既没有对照组，
#       也会因为"找不到 finance/generic"而失败。
#       留在产物里会让使用者以为项目坏了。
BACKEND_SKIP_IN_SUBDIR = {
    "scripts/extract.py",
    "tests/test_domain_isolation.py",
}

# 复制时跳过的目录名 / 文件后缀（运行时产物、缓存、本地产物）
# 注意：这里**不能**放 "data"。
#   backend/data/      → 上传文件与测试库，是运行时产物，不该带（但它不在
#                        BACKEND_ENTRIES 里，本来就不会被复制）
#   backend/app/data/  → 敏感词表 sensitive_words.txt，是**必需的资源文件**，
#                        漏了它内容过滤会失效（导出后测试立刻失败）。
#   用名字一刀切会把两者都排掉，曾经就是这么踩的坑。
SKIP_DIRS = {
    "__pycache__",
    "node_modules",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".git",
    ".venv",
    "venv",
    ".run",
    "tmp",
    "build-logs",
    ".agent-rules",
    "docs",           # 内部文档，不外发
}

SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".tsbuildinfo", ".local"}

# 这些文件不属于交付物：开发辅助、内部资料
SKIP_FILES = {
    "_check_syntax.py",       # 给 AI 用的语法自检脚本，与项目功能无关
    "AGENTS.md",              # AI 工作规则（本地私有）
    "GLOSSARY.md",            # 术语表属于内部文档
}


def _should_skip(path: Path) -> bool:
    """判断某个路径是否要跳过。"""
    if path.name in SKIP_FILES:
        return True
    if path.suffix in SKIP_SUFFIXES:
        return True
    return False


def _copy_tree(
    src: Path,
    dst: Path,
    copied: list[Path],
    exclude: set[str] | None = None,
) -> None:
    """递归复制目录，跳过缓存与运行时产物。

    Args:
        exclude: 额外的排除项（相对 src 的**绝对路径**集合）。

    为什么需要 exclude：
        复制 backend/app 时必须排除 app/domain 整个目录 ——
        领域内容要经过拍平与重写，不能原样复制（否则会把
        finance/generic 等其他领域也带进产物，直接违反隔离要求）。
    """
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.iterdir()):
        if item.is_dir():
            if item.name in SKIP_DIRS:
                continue
            if exclude and item.resolve() in exclude:
                continue
            _copy_tree(item, dst / item.name, copied)
        else:
            if _should_skip(item):
                continue
            shutil.copy2(item, dst / item.name)
            copied.append(dst / item.name)


# ---------------------------------------------------------------------------
# 领域内容的改写
# ---------------------------------------------------------------------------

# 母项目的领域加载入口（app/domain/__init__.py）会引用 KNOWN_DOMAINS，
# 导出后要把它改成"只有自己一个领域、且固定不变"。
_DOMAIN_INIT_TEMPLATE = '''"""当前项目唯一的业务领域：{display_name}。

本文件由母项目的导出脚本生成。

## 与母项目的差别

母项目支持在多个领域之间切换（通过 DOMAIN 环境变量），
本项目**只有 {display_name} 一个领域**，因此：

  · 不需要、也不提供领域切换
  · 其他领域的代码、词表、文案在这里**完全不存在**
  · 你可以在 app/domain/ 里直接修改本领域的内容（身份、禁令、词表）

## 想换领域？

不是改这里，而是回母项目导出另一个领域。
每个导出项目都是独立产品线，不共享代码。
"""

from __future__ import annotations

import logging

from app.domain.spec import DomainSpec  # noqa: F401  （便于扩展时导入）

logger = logging.getLogger("kbqa.domain")

# 本项目只有一个领域。
# 仍然导出 KNOWN_DOMAINS 是为了让 Core 的配置校验能正常工作 ——
# Core 不关心有几个领域，只依赖这个名字存在。
DOMAIN_NAME = "{name}"
DOMAIN_DISPLAY = "{display_name}"
KNOWN_DOMAINS: tuple[str, ...] = (DOMAIN_NAME,)
DEFAULT_DOMAIN = DOMAIN_NAME

_loaded = None


def load_domain(name: str = DOMAIN_NAME) -> DomainSpec:
    """加载本项目的领域。

    参数保留是为了兼容 Core 里可能带参调用；本项目只认 {name}。
    """
    # spec 对象在 domain.py（领域定义），spec.py 是协议类 DomainSpec
    from app.domain.domain import spec

    return spec


def get_domain() -> DomainSpec:
    """取当前领域（Core 各处都通过它拿配置）。"""
    global _loaded
    if _loaded is None:
        _loaded = load_domain()
        logger.info("领域已就绪：%s（%s）", _loaded.name, _loaded.display_name)
    return _loaded


def reset_for_testing() -> None:
    """清空缓存（测试用）。本项目只有一个领域，清空后会重新加载同一个。"""
    global _loaded
    _loaded = None
'''


def _rewrite_domain_package(domain_dir: Path, out_backend: Path, spec_meta: dict) -> list[Path]:
    """把领域模块拍平成 app/domain/，并重写加载入口。

    母项目结构：
        app/domain/
        ├── __init__.py        加载入口（支持多领域）
        ├── base.py            协议
        ├── ecommerce/         ← 要导出的领域
        └── finance/           ← 不要

    导出后结构：
        app/domain/
        ├── __init__.py        新增：单领域加载入口
        ├── spec.py            ← base.py 改名（协议）
        ├── domain.py          ← <name>/domain.py 提上来
        └── tests/             ← <name>/tests/ 提上来

    为什么要拍平：
        产物要让使用者感觉"这就是个单领域项目"。
        留着 app/domain/ecommerce/ 这种一层，会让人以为还有别的领域。
    """
    created: list[Path] = []
    target = out_backend / "app" / "domain"
    target.mkdir(parents=True, exist_ok=True)

    # 1) 协议 base.py -> spec.py（改名后语义更清晰：这是领域规格）
    shutil.copy2(domain_dir.parent / "base.py", target / "spec.py")
    created.append(target / "spec.py")

    # 2) 领域的 domain.py 提上来，并把对 app.domain.base 的引用改成 spec
    src_domain_py = domain_dir / "domain.py"
    content = src_domain_py.read_text(encoding="utf-8")
    content = content.replace("from app.domain.base import", "from app.domain.spec import")
    (target / "domain.py").write_text(content, encoding="utf-8")
    created.append(target / "domain.py")

    # 3) 领域测试提上来
    src_tests = domain_dir / "tests"
    if src_tests.is_dir():
        dst_tests = target / "tests"
        _copy_tree(src_tests, dst_tests, created)
        # 测试里的导入路径要跟着改，同时删掉"锁定 DOMAIN"那行。
        #
        # 为什么要删：母项目里领域测试必须自己声明 DOMAIN（否则会加载 generic 兜底）；
        # 但导出项目只有一个领域，DOMAIN 固定，不需要也不应该再改环境变量。
        # 保留这行的后果很隐蔽：它用 setdefault 写入了 os.environ，
        # 会影响同一进程内后续运行的其他测试 —— 表现为"单独跑全过、一起跑报错"，
        # 极难排查（这个坑在导出验证时真实踩到过）。
        for f in dst_tests.rglob("*.py"):
            t = f.read_text(encoding="utf-8")
            t = t.replace("from app.domain.base import", "from app.domain.spec import")
            # 逐行过滤掉设置 DOMAIN 环境变量的语句
            kept = [
                ln
                for ln in t.split(chr(10))
                if "os.environ.setdefault" not in ln
                and 'os.environ["DOMAIN"]' not in ln
            ]
            t = chr(10).join(kept)
            f.write_text(t, encoding="utf-8")
    else:
        (target / "tests").mkdir(exist_ok=True)
    (target / "tests" / "__init__.py").touch(exist_ok=True)

    # 4) 新的加载入口
    (target / "__init__.py").write_text(
        _DOMAIN_INIT_TEMPLATE.format(**spec_meta), encoding="utf-8"
    )
    created.append(target / "__init__.py")

    return created


def _fix_domain_imports(out_backend: Path, created: list[Path]) -> None:
    """把产物里所有 `from app.domain.base import` 改成 `from app.domain.spec import`。

    母项目里协议叫 base.py，产物里改叫 spec.py。
    不统一改的话，导出项目一导入就 ModuleNotFoundError。
    """
    for py in (out_backend / "app").rglob("*.py"):
        t = py.read_text(encoding="utf-8")
        if "app.domain.base" in t:
            py.write_text(
                t.replace("app.domain.base", "app.domain.spec"), encoding="utf-8"
            )


def _pin_domain_config(out_backend: Path, name: str) -> None:
    """把 config.py 里的 domain 配置固定成本领域，并去掉多领域校验。

    母项目允许 DOMAIN 在多个值之间切换；产物只有一个领域，
    保留切换能力只会让使用者困惑（"我改了这个值会发生什么？"）。
    """
    cfg = out_backend / "app" / "core" / "config.py"
    t = cfg.read_text(encoding="utf-8")

    # 1) 默认值固定成本领域
    t = re.sub(
        r'(    domain: str = )"generic"',
        rf'\1"{name}"',
        t,
        count=1,
    )

    # 2) 去掉"生产环境不能用 generic"的校验（产物里没有 generic 这个选项了）
    t = re.sub(
        r'        if self\.is_prod and self\.domain == "generic":\n'
        r'            raise ValueError\(\n'
        r'                "生产环境（APP_ENV=prod）必须显式指定 DOMAIN，不能使用兜底的 generic。"\n'
        r'                "请设置为 ecommerce / finance 等具体领域。"\n'
        r'            \)\n',
        "",
        t,
    )

    cfg.write_text(t, encoding="utf-8")


def _pin_run_tests_domain(out_backend: Path, name: str) -> None:
    """把 run_tests.py 的兜底领域从 generic 改成实际领域。

    为什么需要：
        母项目的 run_tests.py 在未设置 DOMAIN 时回退 generic（兜底领域）。
        导出项目只有一个领域、也没有 generic 这个选项，
        不修正会导致测试跑在错误的领域上 —— 表现为"领域测试 0 个"、
        以及 Core 测试里与领域相关的部分批量失败。
    """
    f = out_backend / "run_tests.py"
    if not f.exists():
        return
    t = f.read_text(encoding="utf-8")
    t = t.replace(
        'return (os.environ.get("DOMAIN") or "generic").strip().lower()',
        f'return (os.environ.get("DOMAIN") or "{name}").strip().lower()',
    )

    # 领域测试路径也要改：导出后领域被拍平到 app/domain/，
    # 不再有 app/domain/<name>/ 这一层。
    # 不改的话 _collect_domain_tests 找不到目录，会静默返回 0 个用例。
    t = t.replace(
        'tests_dir = HERE / "app" / "domain" / domain / "tests"',
        'tests_dir = HERE / "app" / "domain" / "tests"',
    )

    f.write_text(t, encoding="utf-8")


# 注释里提到"别的领域"的示例片段 -> 替换成中性表述
#
# 为什么清理：导出产物要让人感觉"这就是个单领域项目"。
# 代码注释里反复出现"财经""股票"会让人困惑——"这项目里难道还有财经功能？"
# 注意：只清理**注释与文档**里的示例，不动任何功能性代码
# （比如 domain/domain.py 里 foreign_hints 的财经词是**必须保留**的，见下方白名单）。

# 这些文件里的"别的领域词"是**功能性配置**，绝不能清（清了会破坏跨领域拦截）

def _clean_foreign_mentions(out_backend: Path) -> int:
    """清理注释/文档里对"其他领域"的举例，返回清理的文件数。

    只处理 .py 文件，且只动注释与文档字符串里的示例，不碰功能性代码。

    ⚠️ 必须按**行**处理，不能整块字符串替换。
       原因：这些文件是 CRLF 换行（windows 常见），
       而 Python 源码里的多行字符串是 
，
       整块替换会**完全匹配不上**，而且不会报错 —— 静默失效。
       第一版就是这么失败的：跑了"清理"却一个文件都没改。
    """
    cleaned = 0
    # 同时扫 app/ 和 tests/ ——
    # 第一版只扫了 app/，结果 tests/ 里的说明文字留了下来
    # （测试文件里也会讲到"母项目有多个领域"）。
    targets = list((out_backend / "app").rglob("*.py")) + list(
        (out_backend / "tests").rglob("*.py")
    )
    for py in targets:
        rel = py.relative_to(out_backend).as_posix()
        if rel == "app/domain/domain.py":
            # 领域定义文件：foreign_hints 里**必须**保留其他领域的词，
            # 那是功能性的（用来挡住问错库的情况），不能清。
            continue

        raw = py.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue

        # 用 chr() 构造换行符，避免在源码里写转义序列
        # （本项目多次因转义被工具链吞掉而出错）
        LF_CH = chr(10)
        CRLF_CH = chr(13) + chr(10)

        had_crlf = CRLF_CH in text
        rows = text.replace(CRLF_CH, LF_CH).split(LF_CH)
        out = []
        changed = False

        for ln in rows:
            new_ln = _clean_one_line(ln)
            if new_ln != ln:
                changed = True
            out.append(new_ln)

        if changed:
            result = LF_CH.join(out)
            if had_crlf:
                result = result.replace(LF_CH, CRLF_CH)
            py.write_bytes(result.encode("utf-8"))
            cleaned += 1

    return cleaned


def _clean_one_line(line: str) -> str:
    """把一行里"其他领域"的举例去掉。

    处理两类内容：
      1. 表格行：| 字段 | 用途 | 电商举例 | 财经举例 |  → 只留前两列
      2. 注释行：把"财经说……""在财经领域问……"这类举例改成中性说法

    ⚠️ 只处理**注释与文档**。domain/domain.py 里的 foreign_hints
       是功能性配置（用来挡住问错库），那个文件整体跳过，不做清理。
    """
    # 其他领域的标志词（出现即认为是"替别的领域举例"）
    foreign = ("财经", "股票", "基金", "市盈率", "招商银行", "茅台", "finance")

    # ---------- 1. 表格行 ----------
    if line.lstrip().startswith("|"):
        if not any(m in line for m in foreign):
            return line
        cells = line.split("|")
        # 形态：['', 字段, 用途, 举例1, 举例2, ''] → 保留前两列
        if len(cells) >= 5:
            return "|".join(cells[:3]) + "|"
        return line

    # ---------- 2. 注释行 ----------
    # 只要含其他领域的标志词就尝试清理（不再限制必须以 # 开头 ——
    # 文档字符串的续行只是普通缩进文本，限制过严会漏掉）。
    if not any(m in line for m in foreign):
        return line

    # 注释里的举例换成中性表述。按从长到短替换，避免半截替换。
    replacements = [
        ("同一套 Core 要能服务电商、财经、医疗等多个领域，",
         "同一套 Core 要能服务多个业务领域，"),
        ("电商说「没找到相关商品资料」，财经说「没找到相关财经数据」，",
         "不同领域对「没找到资料」的措辞不同，"),
        ('电商说"没找到商品资料"，财经说"没找到财经数据"。',
         "不同领域对「没找到资料」的措辞不同。"),
        ("#   财经：有哪些股票 / 招商银行 / 市盈率",
         "#   不同领域的用词完全不同"),
        ("#   电商：有哪些商品 / S10 / 手机", "#   例如：电商用「商品」，其他行业用各自的词"),
        ("例：财经领域里问「介绍一下本商品」—— 词表没有\"本商品\"，",
         "例：在某领域里问别的领域的典型问题 —— 词表里没有那个词，"),
        ('但 foreign_hints 里有"商品"，据此拦住，避免列出一份股票清单。',
         "但 foreign_hints 里有它，据此拦住，避免列出一份不相干的清单。"),
        ("这样无论 `DOMAIN` 是 ecommerce 还是 finance，测试都成立。",
         "这样无论加载哪个领域，测试都成立。"),
        ('这样不依赖"商品"或"股票"这类具体词汇。',
         "这样不依赖任何具体领域词汇。"),
        # 以下是文档字符串内部的续行（不在行首，需单独匹配）
        ("必须领域化：电商说「没找到相关商品资料」，财经说「没找到相关财经数据」，",
         "必须领域化：不同领域对「没找到资料」的措辞不同，"),
        ('没有 foreign_hints 时，在财经领域问「介绍一下本商品」会被判成"列举"——',
         '没有 foreign_hints 时，问其他行业的典型问题会被判成"列举"——'),
        ("这样无论 `DOMAIN` 是 ecommerce 还是 finance，测试都成立。",
         "这样无论加载哪个领域，测试都成立。"),
        # 以下三句来自 spec.py 的 docstring，之前因措辞不同没匹配上
        ("因为财经词表里有「介绍一下」，而\"本商品\"不在财经的具体词表里，",
         "因为本领域词表里有通用词，而另一行业的词不在本领域的具体词表里，"),
        ("没有被排除，最终给用户列出一份股票清单，完全答非所问。",
         "没有被排除，最终给用户列出一份完全不相干的清单，答非所问。"),
        ("在财经领域问「介绍一下本商品」会被判成\"列举\"——",
         "问其他行业的典型问题会被判成\"列举\"——"),
        # test_open_intent_logic.py 里对比"母项目多领域"的说明
        ("## 这个文件和电商领域的测试有什么区别", "## 本文件与本领域测试有什么区别"),
        ("- `app/domain/ecommerce/tests/test_open_intent.py`：测**电商词表**是否配置正确",
         "- `app/domain/tests/test_open_intent.py`：测**本领域词表**是否配置正确"),
        ('  （"有哪些商品"要命中、"S10"要排除）。那组测试随领域走。',
         "  （本领域的列举词要命中、具体词要排除）。"),
        ("这样无论 `DOMAIN` 是 ecommerce 还是 finance，测试都成立。",
         "这样无论加载哪个领域，测试都成立。"),
        ('这样不依赖"商品"或"股票"这类具体词汇。',
         "这样不依赖任何具体领域词汇。"),
    ]
    for old_s, new_s in replacements:
        line = line.replace(old_s, new_s)
    return line



def _write_domain_samples(domain_dir: Path, out_root: Path, copied: list[Path]) -> None:
    """把领域样例资料复制到产物根目录的 samples/。"""
    src = domain_dir / "samples"
    dst = out_root / "samples"
    if src.is_dir() and any(src.iterdir()):
        _copy_tree(src, dst, copied)
    else:
        dst.mkdir(exist_ok=True)
        (dst / "README.md").write_text(
            "# 样例资料\n\n把可用于测试本知识库的文档放在这个目录，"
            "然后在系统里上传它们即可体验问答。\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# README 生成
# ---------------------------------------------------------------------------

_README_TEMPLATE = """# {project_name}

{display_name}知识库问答系统 —— 基于 RAG（检索增强生成）的智能问答项目。

把{display_name}相关的文档传进知识库，就能用自然语言提问，得到带出处的答案。

---

## ⚠️ 关于本项目

本项目由 **`{mother_name}`** 于 `{exported_at}` 导出，领域为 **{display_name}**。

**导出后即为独立项目**：母项目的后续更新**不会**自动同步到此处。
如果你在母项目中修复了通用引擎的问题，需要手动合并过来（或重新导出后对照修改）。

来源信息见 `.exported-from` 文件。

---

## 🚀 快速开始

```bash
# 1. 启动数据库（需要 Docker Desktop）
docker compose up -d

# 2. 配置环境变量
copy .env.example .env          # Windows
cp .env.example .env            # macOS / Linux
# 然后按 .env 里的注释填 API Key

# 3. 一键启动（Windows）
双击「启动.bat」

# 或手动分别启动
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

cd frontend
npm install
npm run dev
```

启动后打开 <http://127.0.0.1:5173>，用 `.env` 里配置的管理员账号登录。

---

## 📁 目录结构

```
{project_name}/
├── backend/
│   ├── app/
│   │   ├── api/          HTTP 接口
│   │   ├── core/         配置、安全、多租户隔离
│   │   ├── domain/       ★ 本项目唯一的领域（{display_name}）
│   │   │   ├── domain.py  身份 / 禁令 / 意图词表
│   │   │   └── tests/     领域专属测试
│   │   ├── models/       数据表
│   │   └── services/     检索、问答、解析、额度
│   ├── migrations/       数据库迁移
│   └── tests/            通用测试
├── frontend/             Vue3 + Element Plus
├── samples/              样例资料（可直接上传体验）
└── docker-compose.yml    PostgreSQL + Qdrant
```

---

## 🔧 想改这个领域的行为

所有领域相关的内容都在 `backend/app/domain/domain.py`，包括：

| 字段 | 作用 |
|---|---|
| `persona` | 系统身份（"你是{display_name}知识库助手"） |
| `audience` | 回答面向谁，影响措辞深浅 |
| `forbidden` | 合规禁令，写得越具体越有效 |
| `no_hit_message` | 检索不到资料时的话术 |
| `open_intent_keywords` | 「有哪些 XX」这类列举意图词 |
| `specific_hints` | 具体对象词（命中则不列清单） |
| `foreign_hints` | 其他行业的典型词（挡住问错库的情况） |

改完重启后端即可生效。改这些**不需要动 Core 任何代码**。

---

## 🧪 测试

```bash
cd backend
python run_tests.py
```

会跑「通用测试 + 本领域专属测试」两部分。

---

## 📄 License

{license_line}

---

*本项目由 `{mother_name}` 导出，请遵守原项目的许可协议。*
"""


def _write_readme(
    out_root: Path, project_name: str, meta: dict, mother_name: str
) -> None:
    license_line = "MIT（详见 LICENSE 文件）"
    lic = out_root / "LICENSE"
    if lic.exists():
        first = lic.read_text(encoding="utf-8").splitlines()
        if first and "MIT" in first[0]:
            license_line = "MIT（详见 LICENSE 文件）"

    (out_root / "README.md").write_text(
        _README_TEMPLATE.format(
            project_name=project_name,
            mother_name=mother_name,
            license_line=license_line,
            **meta,
        ),
        encoding="utf-8",
    )


def _write_export_metadata(out_root: Path, name: str, meta: dict, mother_name: str) -> None:
    """写入来源元数据，便于日后追溯「这个项目是从哪导出的」。"""
    (out_root / ".exported-from").write_text(
        "\n".join(
            [
                f"mother_project={mother_name}",
                f"domain={name}",
                f"domain_display={meta['display_name']}",
                f"exported_at={meta['exported_at']}",
                "note=导出后为独立项目，母项目的后续更新不会自动同步。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

KNOWN = ("ecommerce", "finance", "generic")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="从母项目裁剪出只含单一领域的独立项目",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("domain", help=f"要导出的领域，可选：{', '.join(KNOWN)}")
    ap.add_argument("target", help="输出目录（必须在母项目之外）")
    ap.add_argument(
        "--dry-run", action="store_true", help="只显示会复制什么，不写文件"
    )
    ap.add_argument(
        "--force", action="store_true", help="目标目录已存在时覆盖"
    )
    args = ap.parse_args()

    name = args.domain.strip().lower()
    if name not in KNOWN:
        print(f"[错误] 未知领域 {name!r}。可选：{', '.join(KNOWN)}", file=sys.stderr)
        return 2

    domain_dir = BACKEND / "app" / "domain" / name
    if not domain_dir.is_dir():
        print(f"[错误] 领域目录不存在：{domain_dir}", file=sys.stderr)
        return 2

    out_root = Path(args.target).resolve()

    # 安全检查：不允许导出到母项目内部，否则会自我递归复制
    try:
        out_root.relative_to(REPO_ROOT)
        print(
            f"[错误] 输出目录不能位于母项目内部：{out_root}\n"
            f"       母项目：{REPO_ROOT}\n"
            f"       请指定外部路径，例如 ../kbqa-{name}",
            file=sys.stderr,
        )
        return 2
    except ValueError:
        pass  # 不在母项目内，正常

    if out_root.exists():
        if not args.force:
            print(
                f"[错误] 目标目录已存在：{out_root}\n"
                f"       请换个路径，或加 --force 覆盖（会先删除该目录！）",
                file=sys.stderr,
            )
            return 2
        if not args.dry_run:
            print(f"[提示] 覆盖模式：先删除 {out_root}")
            shutil.rmtree(out_root)

    # 读取领域元信息
    sys.path.insert(0, str(BACKEND))
    from app.domain import load_domain

    spec = load_domain(name)
    meta = {
        "name": name,
        "display_name": spec.display_name,
        "exported_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    project_name = f"kbqa-{name}"
    mother_name = REPO_ROOT.name

    if args.dry_run:
        print(f"[dry-run] 领域：{spec.display_name}（{name}）")
        print(f"[dry-run] 输出：{out_root}")
        print(f"[dry-run] 顶层文件：{', '.join(TOP_LEVEL_FILES)}")
        print(f"[dry-run] 顶层目录：{', '.join(TOP_LEVEL_DIRS)}")
        print(f"[dry-run] 后端条目：{', '.join(BACKEND_ENTRIES)}")
        print(f"[dry-run] 领域内容：{domain_dir}")
        return 0

    print(f"=== 导出 {spec.display_name} → {out_root} ===")
    copied: list[Path] = []

    # 1) 顶层文件 + 目录
    out_root.mkdir(parents=True, exist_ok=True)
    for f in TOP_LEVEL_FILES:
        src = REPO_ROOT / f
        if src.exists():
            shutil.copy2(src, out_root / f)
            copied.append(out_root / f)
    for d in TOP_LEVEL_DIRS:
        src = REPO_ROOT / d
        if src.is_dir():
            _copy_tree(src, out_root / d, copied)

    # 2) 后端
    out_backend = out_root / "backend"
    out_backend.mkdir(parents=True, exist_ok=True)
    # app/domain 必须排除：它由第 3 步单独处理（拍平 + 只保留目标领域）。
    # 若不排除，会把 finance/generic 等其他领域原样复制进产物，
    # 直接违反"导出产物不含其他领域"这一核心要求。
    domain_exclude = {(BACKEND / "app" / "domain").resolve()}
    for entry in BACKEND_ENTRIES:
        src = BACKEND / entry
        if not src.exists():
            continue
        if src.is_dir():
            excl = domain_exclude if entry == "app" else None
            _copy_tree(src, out_backend / entry, copied, exclude=excl)
        else:
            shutil.copy2(src, out_backend / entry)
            copied.append(out_backend / entry)

    # 2.5) 删掉产物里不该出现的文件（母项目专用工具）
    for rel in BACKEND_SKIP_IN_SUBDIR:
        f = out_backend / rel
        if f.exists():
            f.unlink()
            copied = [c for c in copied if c != f]

    # 3) 领域：拍平 + 重写加载入口
    created = _rewrite_domain_package(domain_dir, out_backend, meta)
    copied.extend(created)
    _fix_domain_imports(out_backend, created)
    _pin_domain_config(out_backend, name)
    _pin_run_tests_domain(out_backend, name)
    n_cleaned = _clean_foreign_mentions(out_backend)
    if n_cleaned:
        print(f"[清理] {n_cleaned} 个文件里的其他领域举例已中性化")

    # 4) 样例资料
    _write_domain_samples(domain_dir, out_root, copied)

    # 5) README + 来源元数据
    _write_readme(out_root, project_name, meta, mother_name)
    _write_export_metadata(out_root, name, meta, mother_name)

    # 6) 结果汇报
    total = len(copied)
    print(f"\n完成：复制 {total} 个文件")
    print(f"输出目录：{out_root}")
    print("\n下一步：")
    print(f"  cd {out_root}")
    print("  双击「启动.bat」或在 backend 里跑 python -m uvicorn app.main:app --reload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
