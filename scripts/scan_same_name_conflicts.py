"""扫描「同名常量、不同取值」—— 这才是真正会咬人的魔鬼数字。

## 为什么不做「裸数值聚类」

把 `src/` 里所有数值字面量按值聚类，`6.0` 会出现在 69 个模块里，看着吓人，
但它们是**互不相关的**用途（A 处是均线周期、B 处是权重系数）。强行抽成一个
常量只会把两件无关的事绑在一起，比不抽更糟 —— 项目里已把这条取舍写进
`core/market_constants.py`：只有「≥2 个模块用同一个口径」才抽。

## 真正的问题形态

`_JOB_TTL_SECONDS` 在 `api/routes/quant.py` 是 1800、在 `backtest.py` 是 900。
改动方看到这个名字会以为在调全局策略，实际只影响一个路由。
本脚本按**常量名**聚合，把同名不同值的冲突全部列出来。
"""

from __future__ import annotations

import ast
import collections
import pathlib
import sys

SKIP_PARTS = (".venv", "node_modules", "__pycache__", "build", "dist")
#: 已复核、同名但语义确实不同的常量：写明**为什么不合并**。
#: 白名单不是"眼不见为净"，而是把判断固化下来，让脚本只报新出现的冲突。
#:
#: ⚠️ 进白名单前先自问一句：**换个名字是不是更好？**
#: `PRICE_FIELDS` / `DEFAULT_INDEX` / `DEFAULT_TIMEOUT` 原本也在这里，
#: 但它们同名会让人 import 错对象（"手"vs"股"、元组 vs str、子进程 1800s vs HTTP 25s），
#: 换个名字成本几分钟、收益是消除一类静默错误 —— 所以那三个**改名**而不是白名单。
#: 留下的这些是"同名不同值"的最优解就是同名：接口约定（`AGENT_ID`）、
#: 各源独立标定的调参（`_TIMEOUT` / `_HIST_DAYS`）、以及方言本就不同（`_SCHEMA`）。
ACKNOWLEDGED: dict[str, str] = {
    "AGENT_ID": "每个 Agent 自己的标识，同名是接口约定，值当然不同",
    "SYSTEM_PROMPT": "每个 Agent 自己的提示词（`_SYSTEM_PROMPT` 同理）",
    "_SYSTEM_PROMPT": "每个 Agent 自己的提示词",
    "DISCLAIMER": "每个模块的免责声明措辞不同（回测/告警/策略案例各有场景）",
    "_DISCLAIMER": "每个 API 模块的免责声明措辞不同",
    "_SCHEMA": "每个仓储自己的建表 DDL，SQLite 与 PostgreSQL 方言本就不同",
    "TABLE": "每个仓储自己的主表名",
    "RUN_TABLE": "每个仓储自己的运行记录表名",
    "DEFAULT_ROOT": "每个数据集自己的落盘根目录",
    "DEFAULT_CACHE_DIR": "每个缓存自己的目录",
    "_TIMEOUT": "各连接器自己的单次请求超时，按数据源响应速度独立标定",
    "_HIST_DAYS": (
        "各连接器自己的历史回看天数，按指标频率独立标定"
        "（MA50 需 60 天 / 两融只需 10 天）"
    ),
    "_CONTENT_LIMIT": "一个是落库截断（2000 字）、一个是送 LLM 的正文截断（300 字，控 token）",
    "DATASETS": "各模块自己关心的表族（竞价要 3 张日频表、主线要 12 张题材表）",
    "WATCH_TABLE": "各模块自己的自选池表名",
    "_ADDABLE_COLUMNS": "各仓储自己的增量列清单",
    "DEFAULT_TOP": "各模块自己的默认取前 N（资金流 20 / 期货 3），口径无关",
    "DEFAULT_TTL_SECONDS": "幂等键 900s 与人工校验 180s 是两种时效要求，无关",
    "_TASKS_MAX": "各模块自己的并发任务上限，按下游配额独立标定",
    "BUSINESS_PASS": "70.0 与 70 是同一个门槛（float / int 两种写法），无数值分歧",
    "USER_PROMPT": "每个 LLM 调用点自己的提示词（同 `SYSTEM_PROMPT`）",
    # ⚠️ 下面两条**不要"统一"** —— 取值不同是实测选定的，不是笔误。
    # 名字起得太泛（同包内两个模块都用），看到同名会本能想去合并，那会静默降质。
    "BUSINESS_CHARS": (
        "主营文本截断字符数，两个调用点的 prompt 不同、实测最优值不同："
        "`member_pure.py` 的消融实验记录 300→平均差 44.2 / 900→6.7 / 2400→5.6（取 2400），"
        "`relevance.py` 取 900。**合并会降质**"
    ),
    "SCORE_MAX_TOKENS": (
        "输出上限，按 tier 分级是实测踩出来的：`member_pure.py` 用 "
        "`max_tokens_for(tier)` 区分（decision 1024 / reasoning 16384），"
        "`relevance.py` 取 32768 作硬上限。两者口径不同，**不要统一**"
    ),
}


def literal_of(node: ast.expr) -> object | None:
    """取常量右值；算不出来（含表达式）返回 None。"""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = literal_of(node.operand)
        if isinstance(inner, (int, float)):
            return -inner
    if isinstance(node, ast.Tuple):
        items = [literal_of(element) for element in node.elts]
        if all(item is not None for item in items):
            return tuple(items)
    return None


def scan(root: pathlib.Path) -> dict[str, dict[str, object]]:
    """常量名 → {模块路径: 取值}。只收全大写名字（模块级常量约定）。"""
    table: dict[str, dict[str, object]] = collections.defaultdict(dict)
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        mod = str(path).replace("\\", "/")
        for node in tree.body:  # 只看模块级，函数内的局部量不属于「口径」
            targets: list[str] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    targets = [target.id]
                    value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
                value = node.value
            elif isinstance(node, ast.AugAssign):
                continue
            if value is None:
                continue
            for name in targets:
                if not (name.isupper() or (name.startswith("_") and name[1:].isupper())):
                    continue
                literal = literal_of(value)
                if literal is None:
                    continue
                table[name][mod] = literal
    return table


def render(name: str, mods: dict[str, object]) -> None:
    """打印一个冲突常量的全部取值与来源模块。"""
    print(f"⚠️  {name}")
    grouped: dict[str, list[str]] = collections.defaultdict(list)
    for mod, value in sorted(mods.items()):
        grouped[repr(value)].append(mod)
    for value, paths in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        short = [p.replace("src/", "").replace("/", " / ") for p in paths]
        shown = ", ".join(short[:4])
        print(f"      = {value:<16} ({len(paths)} 处) {shown}"
              + (" …" if len(short) > 4 else ""))
    print()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_all = "--all" in sys.argv
    root = pathlib.Path(args[0] if args else "src")
    table = scan(root)
    conflicts = {
        name: mods
        for name, mods in table.items()
        if len({repr(value) for value in mods.values()}) > 1
    }
    reviewed = sorted(name for name in conflicts if name in ACKNOWLEDGED)
    pending = sorted(name for name in conflicts if name not in ACKNOWLEDGED)

    print(f"模块级常量名总数: {len(table)}")
    print(f"同名不同值: {len(conflicts)}"
          f"（已复核白名单 {len(reviewed)}，**待处理 {len(pending)}**）\n")

    if not pending:
        print("✅ 没有待处理项。新增共享口径请登记到 "
              "`tests/unit/test_market_constants.py::SHARED`。\n")
    else:
        for name in pending:
            render(name, conflicts[name])

    if show_all:
        print(f"—— 全部 {len(conflicts)} 项明细（含白名单）——\n")
        for name in sorted(conflicts):
            render(name, conflicts[name])
        return 0

    print("—— 已复核白名单（`--all` 看取值明细）——")
    for name in reviewed:
        print(f"   {name}: {ACKNOWLEDGED[name]}")
    # 只作提示，不当作失败：本项目多数同名常量是**有意的本地口径**，
    # 硬性失败会逼着人往白名单里塞东西，反而失去信号。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
