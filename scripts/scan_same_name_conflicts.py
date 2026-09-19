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
    "DEFAULT_INDEX": "一个是 (代码, 名称) 元组、一个是纯代码，用法不同",
    "DEFAULT_TIMEOUT": "一个是子进程总超时（秒级）、一个是单次 HTTP 超时，量纲场景都不同",
    "_TIMEOUT": "各连接器自己的单次请求超时，按数据源响应速度独立标定",
    "_HIST_DAYS": "各连接器自己的历史回看天数，按指标频率独立标定",
    "_CONTENT_LIMIT": "一个是落库截断、一个是提示词截断，下游消费方不同",
    "PRICE_FIELDS": (
        "两个数据源的列名口径不同：仓库面板用 Tushare 的 `volume_lot`（手），"
        "价格面板用 DataPoint.extra 的 `volume`（股）—— 不能合并，合并即 KeyError"
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


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "src")
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
              "`tests/unit/test_market_constants.py::SHARED`。")
        return 0

    for name in pending:
        mods = conflicts[name]
        print(f"⚠️  {name}")
        grouped: dict[str, list[str]] = collections.defaultdict(list)
        for mod, value in sorted(mods.items()):
            grouped[repr(value)].append(mod)
        for value, paths in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
            short = [p.replace("src/", "") for p in paths]
            print(f"      = {value:<14} ({len(short)} 处) {', '.join(short[:3])}"
                  + (" …" if len(short) > 3 else ""))
        print()

    print("—— 已复核（不重复列出，理由见脚本 ACKNOWLEDGED）——")
    for name in reviewed:
        print(f"   {name}: {ACKNOWLEDGED[name]}")
    # 只作提示，不当作失败：本项目多数同名常量是**有意的本地口径**，
    # 硬性失败会逼着人往白名单里塞东西，反而失去信号。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
