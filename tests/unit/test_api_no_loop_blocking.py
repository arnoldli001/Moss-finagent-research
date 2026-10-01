"""「阻塞调用不许上事件循环」的源码守卫（2026-09-27 线上事故复盘）。

## 现场

用户报障：「首次登录进去，事件告警有，而**策略回测、主线挖掘、资金流监控**
都要等很久都不出来」，并伴随间歇性的「后端服务当前不可达」。

真因不在那三个面板，而在**「策略回测」首屏顺手拉的一个体检接口**：

    GET /api/v1/quant/data-status        （routes/quant.py 的 data_status）

它的活全是同步阻塞的 —— 逐数据集 `iterdir()` 扫分区、读 manifest 算 coverage、
再 `warehouse_status()` 对仓库每张表做 `MIN/MAX(日期)`（无索引 = 全表扫描）——
却写在 `async def` 里，于是**跑在事件循环线程上**。单进程 uvicorn 只有一个循环：

  · 首屏那十几个并发请求全部排队等循环（实测风暴期间 `/health/live`
    最慢一次 **12.75 秒**，而它的判死线是 **3 秒**）→ 弹「后端服务当前不可达」；
  · 其它页签（主线挖掘 / 资金流监控）也跟着一起不出来 —— 它们没做错什么，
    只是排在后面。

证据是请求风暴期间连打 4 次 `py-spy dump`，MainThread 每次都停在这条链上：

    stat → is_file → keys (dataset_store.py:127)
    coverage (dataset_store.py:186) → data_status (quant.py:92)
    stats (warehouse.py:833) → warehouse_status (warehouse.py:1454)

修法（`routes/quant.py`）：① 整段挪进 `asyncio.to_thread`；② 结果缓存 60 秒
（数据条是"看一眼"的结论，前端每次开面板都会拉）。

## 这个文件守什么

前端没有单测框架、后端也不会因为"把阻塞活留在 `async def` 里"而报错 ——
它只在**并发**时才现形，本地单人点一遍完全看不出来。所以这里用 AST 断言
把不变式钉住（**只看路由函数自己的语句**：丢进 `to_thread` 的嵌套函数不算）：

1. `data_status` 路由体内不许出现同步体检调用；
2. 它必须用 `asyncio.to_thread` 调 `_collect_data_status`；
3. 体检实体必须是**同步**函数（`def`，不是 `async def`）；
4. 结果要有 TTL 缓存；
5. `quick_ic` 的目录扫描同样在线程里；
6. 全局：任何 `async def` 路由都不许直接调已知阻塞函数。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
QUANT = SRC / "api" / "routes" / "quant.py"

#: 已知的同步阻塞调用：出现在 `async def` 路由**自己的语句**里就是事故
#:
#: ★ `CHG-0146` 补入后 5 个：它们各自都在 `async def` 里堵过事件循环
#: （实测 3,056.8 / 3,766.7 / 679.1 / 179.7 / 116 ms，见文件末尾那条判据的说明）。
BLOCKING_CALLS = frozenset({
    "warehouse_status",
    "coverage",
    "iterdir",
    "glob",
    "keys",
    # ↓ `CHG-0146`：本轮实测定罪的四个 + 顺带修掉的一个
    "build_cycle",          # 情绪周期：同步读行情仓做聚合
    "_data_status",         # 主线本地仓：多表行数 + 同步台账
    "_relevance_stats_sync",  # 提纯状态：同步 SQLite JOIN + 聚合
    "get_preheat",          # 竞价预热摘要：取数/落库
    "data_watermark",       # 主线数据水位线：同步 SQL MAX（面板首个请求会走）
})


def _module() -> ast.Module:
    return ast.parse(QUANT.read_text(encoding="utf-8"))


def _functions(tree: ast.Module) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    return {node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _own_calls(node: ast.AST) -> set[str]:
    """函数**自己语句**里调用的名字集合。

    ⚠️ 三条都要注意，否则会误报（第一版全踩了）：
      · **不进嵌套函数**：`def work()` 里的 `store.keys()` 是要丢进
        `asyncio.to_thread` 的，算进来会把正确的写法判成错的；
      · **不看 docstring**：本次事故的复盘就写在 docstring 里，
        里面自然会出现 `warehouse_status()` 这些词；
      · **不看注释**（AST 天然不看）。
    """
    names: set[str] = set()

    def visit(current: ast.AST) -> None:
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda, ast.ClassDef)):
                continue                     # 嵌套定义：由调用方负责
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
            visit(child)

    for statement in _own_body(node):
        # ⚠️ 语句本身是 `def` 时也要跳过：`visit()` 只挡"子节点里的 def"，
        #    直接把 `def work()` 传进去照样会遍历到它的函数体（第一版就栽在这）。
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        visit(statement)
    return names


def _own_body(node: ast.AST) -> list[ast.stmt]:
    """函数自己的语句（去掉 docstring）。"""
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return body


#: 豁免标记：放在**出事那一行**（或它上一行）即跳过该调用。
#:
#: ## 为什么需要它（`CHG-0146` 扩范围时当场出现）
#:
#: 判据是**按名字**匹配的，而名字会撞车：`snapshot.keys()`（字典键，O(1)）
#: 与 `store.keys()`（扫目录，阻塞）同名。第一版扩到全部路由文件后，
#: `fundflow.py::search` 立刻被误报。把 `keys` 从表里删掉是**更糟**的解法
#: （那正是当年 `quant.py` 事故的那个调用）——所以给一个**必须带理由**的豁免：
#: 写 `# loop-blocking-ok: <理由>`，判据看得到这行注释、并在报告里留痕。
LOOP_BLOCKING_OK = "# loop-blocking-ok"


def _own_calls_with_lines(node: ast.AST, source: str) -> dict[str, int]:
    """函数自己语句里调用的名字 → 行号（供豁免标记按行判定）。"""
    lines = source.splitlines()
    found: dict[str, int] = {}

    def visit(current: ast.AST) -> None:
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else None)
                if name:
                    lineno = getattr(child, "lineno", 0)
                    text = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
                    prev = lines[lineno - 2] if lineno >= 2 else ""
                    if LOOP_BLOCKING_OK not in text and LOOP_BLOCKING_OK not in prev:
                        found.setdefault(name, lineno)
            visit(child)

    for statement in _own_body(node):
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        visit(statement)
    return found


def _own_names(node: ast.AST) -> set[str]:
    """函数自己语句里**出现过**的名字（含作为参数被传递的函数名）。

    `asyncio.to_thread(_collect_data_status, ...)` 里的 `_collect_data_status`
    是 `Name` 载入而非调用，`_own_calls()` 看不到它 —— 所以要单独收一遍名字。
    """
    names: set[str] = set()
    for statement in _own_body(node):
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.walk(statement):
            if isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Attribute):
                names.add(child.attr)
    return names


def test_data_status_route_does_not_do_blocking_work():
    """`/data-status` 路由体里不许直接扫目录/查仓库 —— 必须整段在线程里跑。"""
    route = _functions(_module()).get("data_status")
    assert route is not None, "找不到 async def data_status"
    hits = sorted(_own_calls(route).intersection(BLOCKING_CALLS))
    assert not hits, (
        f"data_status 路由体里出现了同步阻塞调用 {hits} —— 它会占住事件循环，"
        "让首屏所有其它请求排队（2026-09-27 线上事故）。"
        "把这些活放进 `_collect_data_status`，再用 `asyncio.to_thread` 调它。")


def test_data_status_route_offloads_to_thread():
    """路由必须真的把体检丢进线程，并调用体检实体。"""
    route = _functions(_module()).get("data_status")
    assert route is not None, "找不到 async def data_status"
    calls = _own_calls(route)
    names = _own_names(route)
    assert "to_thread" in calls, (
        "data_status 没有把体检丢进线程（缺 `asyncio.to_thread`）—— "
        "阻塞 I/O 会占住事件循环")
    assert "_collect_data_status" in names, (
        "data_status 没有把体检实体交给 `asyncio.to_thread` 执行")


def test_collector_is_synchronous():
    """体检实体必须是同步 `def`：写成 `async def` 会诱使人在循环里直接 await。"""
    funcs = _functions(_module())
    assert "_collect_data_status" in funcs, "找不到 _collect_data_status"
    assert not isinstance(funcs["_collect_data_status"], ast.AsyncFunctionDef), (
        "_collect_data_status 必须是同步函数（`def`，不是 `async def`）")


def test_data_status_result_is_cached():
    """结果必须带 TTL 缓存：前端每次打开面板都会拉它。"""
    src = QUANT.read_text(encoding="utf-8")
    assert "_DATA_STATUS_TTL" in src and "_DATA_STATUS_CACHE" in src, (
        "data_status 没有结果缓存 —— 每次开面板都要重扫目录 + 逐表 MIN/MAX")
    ttl = re.search(r"_DATA_STATUS_TTL\s*=\s*([0-9.]+)", src)
    assert ttl and float(ttl.group(1)) >= 60, (
        "TTL 太短：体检最贵那一步是仓库逐表 MIN/MAX（实测 7.18s，1 亿行、无索引），"
        "而数据条一天只变一次 —— 至少给到分钟级")


def test_warm_uses_the_same_cache_key_as_the_route():
    """启动预热必须与路由用**同一套默认值**，否则预热等于没做。

    第一版 `_warm_quant_data_status` 写死 `root="data/quant"`，而路由默认是
    `DEFAULT_ROOT = "data/quant/tushare"` → 缓存键对不上，第一个用户照样等 11 秒，
    而日志里看不出任何异常。这条断言就是防它再犯。
    """
    funcs = _functions(_module())
    warm = funcs.get("warm_data_status")
    assert warm is not None, "找不到 warm_data_status（启动预热入口）"
    src = QUANT.read_text(encoding="utf-8")
    sig = re.search(r"async def warm_data_status\((.*?)\)\s*->", src, re.S)
    assert sig, "解析不出 warm_data_status 的签名"
    assert "DEFAULT_ROOT" in sig.group(1), (
        "warm_data_status 的 root 默认值没有用 `DEFAULT_ROOT` —— "
        "手写字面量会与路由的默认值漂移，预热静默失效")
    assert not re.search(r'root\s*:\s*str\s*=\s*"', sig.group(1)), (
        "warm_data_status 的 root 默认值是写死的字符串 —— 必须引用 DEFAULT_ROOT")


def test_quant_ic_also_offloads_directory_scan():
    """`/ic` 同样把 `store.keys()`（扫目录）挪进了线程 —— 别再挪回循环里。"""
    route = _functions(_module()).get("quick_ic")
    assert route is not None, "找不到 async def quick_ic"
    calls = _own_calls(route)
    assert "to_thread" in calls, "/ic 没有把重活丢进线程"
    assert "keys" not in calls, (
        "/ic 的 `store.keys()` 又跑回事件循环上了 —— 它是阻塞 I/O，"
        "要放在 `work()` 里由 `asyncio.to_thread` 执行")


def test_no_async_route_calls_blocking_helpers_directly():
    """全局扫描：**全部路由文件**里，任何 `async def` 路由都不许直接调阻塞函数。

    ## ★ `CHG-0146`：这条判据的**范围**曾经是它的漏洞

    第一版只 `ast.parse(routes/quant.py)` + 5 个名字（`warehouse_status`/`coverage`/
    `iterdir`/`glob`/`keys`）⇒ 它守着 `quant.py`，而同一类缺陷在别的路由文件里
    **长驱直入**。实测（2026-09-30，进程内 harness 定罪）：

      · `routes/auction_select.py::sentiment_cycle` → `build_cycle()` 同步读行情仓
        ⇒ **堵事件循环 3,056.8 ms**；
      · `routes/mainline.py::data_status` → `_data_status()` 同步读本地仓
        ⇒ 冷路径 **3,766.7 ms**；
      · `routes/mainline.py::relevance_stats` → 同步 SQLite（JOIN + 聚合）**679.1 ms**；
      · `routes/auction_select.py::preheat` → `scheduler.get_preheat()` **179.7 ms**。

    四个全在 `quant.py` 之外 ⇒ **判据守的那一格，恰好不是出事的那一格**。

    ## 名字表只能挡已知的，行为判据才兜底

    这份清单是"已知阻塞函数名"，写不进表里的（比如某个新的同步取数函数）
    它抓不到 —— 真正兜底的是 `tests/integration/test_no_loop_blocking_endpoints.py`
    的**行为**判据（20 ms 心跳直接量"谁把循环按住了"，与名字无关）。
    两条一起才完整：**这条便宜、能指认名字；那条贵、但能抓没见过的**。
    """
    files = sorted((SRC / "api" / "routes").glob("*.py"))
    assert len(files) >= 10, (
        f"只扫到 {len(files)} 个路由文件 —— 扫描范围又缩回去了（本条判据的全部价值在范围）")
    offenders: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:      # 语法错误由别的判据报，这里不重复
            offenders.append(f"{path.name}: 解析失败 {exc.msg}")
            continue
        for name, node in _functions(tree).items():
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            hits = _own_calls_with_lines(node, text)
            bad = sorted(n for n in hits if n in BLOCKING_CALLS)
            if bad:
                where = ", ".join(f"{n}@{hits[n]}" for n in bad)
                offenders.append(f"{path.name}::{name}: [{where}]")
    assert not offenders, (
        "这些 async 路由在事件循环上直接调用同步阻塞函数（要丢进 "
        "`await asyncio.to_thread(...)`）：" + "；".join(offenders))
