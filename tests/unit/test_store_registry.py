"""存储清单护栏（PRD §16 / §18，`CHG-0066`）—— **三条防复发判据**。

## 为什么需要它（一次真实报障）

用户报障「pilot 投研取数查不到，数据分散到多个库里」。根因不是"数据分散"，
而是**没有任何一层认全部**：三份清单各用不同的枚举方式、各漏一块 ——
`assets` 只扫一个库、`ColumnIndex` 按名字 + 体积**双重跳过**行情仓、
`TABLE_FREQUENCY` 是手写的。行情仓 1,542 万行日线因此**从未进过任何清单**。

所以本文件钉住三件事，**每条都对应一个曾经真实发生的失效**：

| 判据 | 钉住什么 | 失效长什么样 |
|---|---|---|
| ① 库清单覆盖实扫结果 | 新增一个库却不登记 | 清单继续"通过"，而新库**永不被扫**（静默假绿） |
| ② 受保护存储 | `warehouse.db` 被清理口径误删 | 14.36 GiB 行情库没了，**前端立刻废** |
| ③ 行数常量 = 0 | "11,837,850" 这类数字被抄进注释 | 抄到 6 处后据此误判"生产上永远取不到" |

外加一条**棘轮**（判据 ④）：`src/` 里的存储路径字面量**只许减不许增**。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.infrastructure.catalog import data_stores as ds

# ======================================================================
# 判据 ④ 的棘轮基线
# ======================================================================
#
# 「存储路径字面量」= `src/` 里**非 docstring** 的字符串常量，它**整串就是一个路径**
# （形如 `data/xxx` 或 `.../data/xxx`）。它们是"库在哪"被写死的地方，
# 本项目的事故基本都是它们造成的（原始 77 处 / 51 文件，见 PRD §16.1）。
#
# ★ **已收敛到 0**（77 → 0，`CHG-0069`/`CHG-0070`/`CHG-0071`），
#   所以原先的棘轮基线常量与"降到 0 后删掉它"的过期断言**都已删除**，
#   只留 `test_no_store_path_literals_in_src` 这条永久栅栏。
#: 唯一允许持有存储字面量的模块：**它要读 YAML，但不是唯一事实源本身**
ALLOWED_LITERAL_MODULES: tuple[str, ...] = ()

_LITERAL_PATTERNS = (re.compile(r"^data[/\\]"), re.compile(r"[/\\]data[/\\]"))


def _is_store_path_literal(value: str) -> bool:
    """这个字符串常量**本身是不是一个存储路径**（不是"提到"了路径）。

    ## 为什么判据要收紧到"整串即路径"（`CHG-0071`）

    第一版判据把**任何**含 `data/...` 的字符串都算进来，于是把
    `scheduler/registry.py` 里给人看的作业说明（
    `"落盘 data/auction_hist/tick_auction_<日期>.parquet"`）、
    以及带空格的中文提示全算成"存储路径字面量"。
    那不是解析点 —— 删掉它只会让运维界面少一句该说的话。

    **判据要适应代码，不要让代码迁就判据。** 所以收紧为：

      · 整串（去空白后）就是一个路径：不含空格、不含中文；
      · 以 `data/` 开头，或 `<盘符>:/.../data/` 形式；
      · **不以 `/` 开头** —— 那是 FastAPI 路由路径（`/data/macro`）。
    """
    t = value.strip().replace("\\", "/")
    if not t or len(t) > 200 or "\n" in t:
        return False
    if any(ch.isspace() for ch in t):        # 有空白 → 是句子，不是路径
        return False
    if any("\u4e00" <= ch <= "\u9fff" for ch in t):   # 有中文 → 是说明文案
        return False
    if t.startswith("/"):                    # 路由路径，不是存储路径
        return False
    if t.startswith("data/"):
        # 光秃秃的 `data/` 是**数据根**，不是某一条存储 —— 它本身没有"库在哪"的含义。
        return bool(t[len("data/"):].strip("/"))
    return any(p.search(t) for p in _LITERAL_PATTERNS)


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _store_path_literals() -> list[tuple[str, int, str]]:
    """`src/` 里所有非 docstring 的存储路径字面量（AST 口径，不用正则扫源码）。"""
    found: list[tuple[str, int, str]] = []
    for f in sorted((ds.PROJECT_ROOT / "src").rglob("*.py")):
        rel = f.relative_to(ds.PROJECT_ROOT).as_posix()
        if rel in ALLOWED_LITERAL_MODULES:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docs and _is_store_path_literal(node.value):
                found.append((rel, node.lineno, node.value))
    return found


# ======================================================================
# 判据 ① 库清单必须覆盖实扫结果
# ======================================================================

def test_registry_covers_every_scanned_database() -> None:
    """`data/**/*.db` 里每一个库都必须被清单覆盖。

    这条判据的价值在于**新增库时立刻红灯**，而不是等某个清单"恰好"扫不到它 ——
    行情仓就是这么被漏掉的（`SKIP_DB_PATTERNS` 里的 `"warehouse.db"` 一行）。
    """
    unregistered = ds.unregistered_databases()
    assert unregistered == [], (
        "发现未登记的 SQLite 库（请加进 configs/data_stores.yaml）：\n"
        + "\n".join(f"  {p}" for p in unregistered))


def test_warehouse_is_scanned_by_the_column_index() -> None:
    """共享行情仓必须**真的进得了**反向索引（回归：它曾被双重跳过）。

    断言到"能查到一个具体列"这一层，而不是"清单里有这条"——
    因为原来的缺陷正是"清单里没有 + 扫描按名字跳过"同时成立，
    只查清单存在性的话，扫描侧的跳过照样会让它消失。
    """
    from src.infrastructure.catalog.column_index import ColumnIndex

    idx = ColumnIndex()
    dbs = {p.name for p in idx.discover_databases()}
    assert "warehouse.db" in dbs, f"行情仓没进扫描范围：{sorted(dbs)}"
    idx.build()
    hits = [h for h in idx.find_column("dv_ratio") if h.db_name == "warehouse.db"]
    assert hits, "反向索引查不到权威库里的 dv_ratio（股息率）"


# ======================================================================
# 判据 ② 受保护存储
# ======================================================================

def test_protected_stores_are_declared_and_intact() -> None:
    """受保护存储必须被声明、存在、且与别的库**不是同一个文件**。

    `warehouse` 是用户 2026-09-28 明确点名"不能随便删，前端要用的"那一个。
    """
    protected = {s.name for s in ds.protected_stores()}
    assert "warehouse" in protected, "行情仓必须是 protected（用户明确声明不得删除）"
    assert "legacy_main" in protected, (
        "遗留主库必须 protected —— 它的 `fact_data_points` 是全项目覆盖最全的"
        "一份（`reset_admin_password.py` 叫它'遗留空库'，那个标签只对认证表成立）")
    for s in ds.protected_stores():
        assert s.exists, f"受保护存储不存在：{s.name} → {s.resolved()}"
    # 两个受保护库不能是同一路径（否则"分别保护"是假象）
    paths = [s.resolved() for s in ds.protected_stores()]
    assert len(set(paths)) == len(paths), f"受保护存储路径重复：{paths}"


def test_protected_store_is_excluded_from_cleanup_scope(monkeypatch) -> None:
    """清理口径不得把受保护存储当目标。

    判据写成机器可读的：受保护存储的**路径**不出现在任何
    "可写且非保护"的声明里；且 `writable_here` 对它们不会给出
    "随便删"的绿灯（`writable=False` 或"主实例专属"都算合格）。
    """
    for s in ds.protected_stores():
        d = ds.writable_here(s.name)
        # 要么登记为只读，要么明确"写者=主实例"（隔离档拿到的是 False）
        assert d.allowed is False or d.reason.startswith("共享存储"), (
            f"{s.name} 的写权限表述没有把风险说清楚：{d.reason}")
    wh = ds.get_store("warehouse")
    # ⚠️ 这条原判据是 `wh.writer == "main"`（"写者必须声明为主实例，
    #    主实例要同步数据"）。2026-09-29 用户改了口径（`CHG-0087`）：
    #    「共享行情仓，dev 读，pilot 写和读；**谁负责更新数据谁有写权限**」
    #    —— 所以"写者叫什么"不该由测试写死，测试要钉的是**性质**：
    #    ① 可写（否则没人能更新行情）；② 写者是一个**具体环境**或主实例；
    #    ③ 非写者的隔离档必须被判为不可写。
    assert wh.writable is True, "行情仓可写必须为真，否则谁都无法更新行情"
    assert wh.writer in (*ds.ISOLATED_ENVS, "main", "own"), (
        f"行情仓的写者声明不合法：{wh.writer!r}")
    writer_env = wh.writer if wh.writer in ds.ISOLATED_ENVS else None
    if writer_env is not None:
        for env in ds.ISOLATED_ENVS:
            if env == writer_env:
                continue
            monkeypatch.setenv("MOSS_ENV", env)
            assert ds.writable_here("warehouse").allowed is False, (
                f"{env} 不是行情仓的写者，必须被判为不可写")


# ======================================================================
# 判据 ③ 行数常量 = 0
# ======================================================================

#: 曾经被抄到 6 处的那些"行数常量"的数字形态。
_ROW_COUNT_CONSTANTS = (
    "11837850", "11837692", "15410692", "15426322", "15426153",
)


def test_no_row_count_constants_in_src() -> None:
    """`src/` 里不得出现形如"11,837,850"的行数常量（含注释与 docstring）。

    ★ 为什么连注释也管：那个数字是**化石副本**的行数，被抄进 6 处注释后，
    `akshare_connector` 据此决定"不走本地、改走网络自算股息率"——
    为一个不成立的前提长期付网络成本。**行数一律现算**。
    """
    offenders: list[str] = []
    for f in sorted((ds.PROJECT_ROOT / "src").rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        digits = re.sub(r"[,\s_]", "", text)
        for const in _ROW_COUNT_CONSTANTS:
            if const in digits:
                offenders.append(f"{f.relative_to(ds.PROJECT_ROOT)} 含 {const}")
    assert offenders == [], (
        "src/ 里出现行数常量（应改为现算或删掉）：\n" + "\n".join(offenders))


# ======================================================================
# 判据 ④ 路径字面量：**已收敛到 0，栅栏改为永久断言**
# ======================================================================
#
# 收敛过程（`CHG-0069` / `CHG-0070` / `CHG-0071`）：77 → 67 → 49 → 36 → 31 → 25 → **0**。
#
# 原先这里有一个 `_LITERAL_BASELINE` 棘轮常量 + 一条"降到 0 后必须删掉它"的
# 过期断言。**现在正是那条过期断言要求的状态** —— 所以基线与过期断言都已删除，
# 只留下面这条永久栅栏（`CHG-0071`）。
#
# 为什么不再保留一个"允许少量"的基线：基线一旦存在，就会有人往里加回一处，
# 而"判据认不出坏掉的形状"正是本文件开头那三条护栏要防的东西。

def test_no_store_path_literals_in_src() -> None:
    """`src/` 里**不得**再出现存储路径字面量（整串即路径的那种）。

    要改路径就改 `configs/data_stores.yaml` —— 那是唯一事实源。
    写在代码里就会重新长出"同一个 key 写在 N 处"的老问题
    （7 个仓储各写一份 `data/moss_finagent.db` 正是这么来的）。
    """
    found = _store_path_literals()
    by_file: dict[str, int] = {}
    for rel, _ln, _v in found:
        by_file[rel] = by_file.get(rel, 0) + 1
    detail = "\n".join(f"  {n:>3}  {r}" for r, n in
                       sorted(by_file.items(), key=lambda kv: -kv[1]))
    assert found == [], (
        f"`src/` 里重新出现了 {len(found)} 处存储路径字面量。\n"
        f"路径一律从 `configs/data_stores.yaml` 取（`resolve_store` / `store_rel`）；"
        f"确实需要新的位置就先在清单里登记一条。\n{detail}")


def test_store_path_literal_judge_recognises_real_paths() -> None:
    """**判据自证**：喂已知的正例/反例，确认它认得出、且不误伤。

    没有这条，上面那条 `== []` 可能因为"判据太严"而永远绿 ——
    那是最隐蔽的一种假绿（把该检出的东西全判成不是路径）。
    """
    positive = ("data/audit", "data/quant/warehouse.db",
                "data/run/free_tier_429.json", "D:/x/data/y.db",
                "data/cache/intel/prewarm_state.json",
                # DSN 里内嵌了硬编码路径 → **应该**被抓到（这也是"路径写死了"）
                "sqlite:///data/moss_finagent.db")
    negative = ("落盘 data/auction_hist/tick_auction_<日期>.parquet",  # 说明文案
                "/data/macro",                                         # 路由路径
                "data/", "")
    for v in positive:
        assert _is_store_path_literal(v), f"正例被判成不是路径：{v!r}"
    for v in negative:
        assert not _is_store_path_literal(v), f"反例被判成路径：{v!r}"


def test_write_decisions_are_reported_with_reasons() -> None:
    """写权限必须给**人话理由**，且"已裁定 / 待裁定"要如实分开标记。

    ★ 2026-09-29（`CHG-0087`）起，`warehouse` **已裁定**（`decided=True`）：
    用户裁定「共享行情仓，dev 读，pilot 写和读；谁负责更新数据谁有写权限」，
    于是 `writer: pilot`，非写者实例**拒写**（不只是提示）。
    仍为 `decided=False` 的是 `writer: main` 那批共享存储（审计项 A7 的剩余部分）
    —— 它们只是报告、不阻断，两者混看会把"还没定"读成"已经管住"。
    """
    for name in ("app_db", "warehouse", "legacy_main", "mainline_cache"):
        d = ds.writable_here(name)
        assert d.reason.strip(), f"{name} 的写权限没有理由"
        assert isinstance(d.decided, bool)
        assert d.to_dict()["store"] == name


# ======================================================================
# L2：跨库只读查询面（PRD §16.3「读统一、写归属」）
# ======================================================================

@pytest.mark.skipif(not ds.get_store("warehouse").exists,
                    reason="本机没有共享行情仓")
def test_cross_store_readonly_query_works_and_rejects_writes() -> None:
    """跨库只读查询必须**能查**、且**写不进**（两个方向都要钉住）。

    只钉"能查"会漏掉写保护；只钉"写不进"会在查询坏掉时照样绿。
    """
    from src.infrastructure.catalog.local_data import LocalDataExecutor

    rows = LocalDataExecutor.query_across(
        ["app_db", "warehouse"],
        "SELECT COUNT(*) AS n FROM warehouse.quant_daily "
        "WHERE trade_date = (SELECT MAX(trade_date) FROM warehouse.quant_daily)")
    assert rows and int(rows[0]["n"]) > 0, "跨库查询没取到任何行"

    conn, aliases = ds.open_readonly("app_db", "warehouse")
    try:
        assert aliases == {"app_db": "main", "warehouse": "warehouse"}
        for stmt in ("INSERT INTO warehouse.quant_daily (trade_date, code) "
                     "VALUES ('19000101','X')",
                     "CREATE TABLE main._guard_probe (x INT)"):
            with pytest.raises(Exception):
                conn.execute(stmt)
    finally:
        conn.close()


def test_available_stores_exposes_registry_view() -> None:
    """执行器给出的存储视图必须与 registry 同源（防止再长出一份清单）。"""
    from src.infrastructure.catalog.local_data import LocalDataExecutor

    view = LocalDataExecutor.available_stores()
    names = {s["name"] for s in view}
    assert names == {s.name for s in ds.all_stores()}
    assert all("note" in s for s in view), "存储视图必须带人话说明"
