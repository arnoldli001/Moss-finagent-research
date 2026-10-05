"""护栏：列候选**单一事实源** + 本地多路径择优（newest-wins）与可见性。

## 这个文件守的两类缺陷

**① 同一个判据被写了 N 份，且逐字不同。**
2026-10-01 实测：实体列候选 2 份（`column_index` 8 项 / `local_data` 6 项）、
时间列候选 3 份（`column_index` 10 项 / `local_data` 9 项 /
`assets` 8 项且**顺序不同**）—— 而 `column_index` 的注释还写着「与 assets 同源同序」。
更值得记的是 `local_data` 那两份**从未被任何代码读取**：
一份不生效的判据 + 一句不成立的"同源"注释 = 一颗看起来在生效的雷。

**② 同一列有多条路径，而系统对"取了哪条、为什么"一个字都没有。**
实测（按 `asset_id` 去重后）**119 个列**有多条路径（`trade_date` 37 张、
`code` 47 张…），而选库只有一个入口、只返回一张表。
期次/口径不同的两张表看起来一样可用 —— 这是"看起来很有据"的错误温床。

## 判据纪律

· **唯一性用 AST 断言"字面量只有一处"**，不是"值相等"——
  值相等但两份拷贝照样会漂（本文件的第①条就是为此）。
· **行为判据优先**：择优的正确性断言"取到的是期次更新的那张"，
  而不是"源码里有 `period_key` 字样"（字符串断言在单进程多模块加载下会假红，
  本项目已登记过）。
· **自证**：把择优关掉（`_rank_key` 退化成只看"有没有数据"）时，
  第③条必须**变红**；且先断言"关掉之后落点确实变了"，否则
  `pytest.raises` 可能因为别的原因通过（假自证）。
· **不依赖本机数据**：全部用临时库自造，避免"开发机绿、CI 红"。
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from src.infrastructure.catalog import column_index as ci
from src.infrastructure.catalog.column_index import (
    ENTITY_COLUMN_CANDIDATES,
    PATH_REASON_TEXT,
    TIME_COLUMN_CANDIDATES,
    ColumnIndex,
    PathReason,
    period_key,
)
from src.infrastructure.catalog.local_data import LocalDataExecutor

CATALOG_DIR = Path(ci.__file__).resolve().parent
OWNED_FILES = ("column_index.py", "local_data.py", "assets.py")
#: 认"这就是候选常量"的标记元素（实体侧 / 时间侧各一个够用的锚）
_ENTITY_MARK = "code"
_TIME_MARKS = ("trade_date", "period_date")


# ============================================================
# 夹具：自造"同一列两条路径"
# ============================================================


def _write_db(path: Path, table: str, ddl: str, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE {table} ({ddl})")
    placeholders = ",".join("?" * len(rows[0]))
    con.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
    con.commit()
    con.close()


@pytest.fixture()
def two_paths(tmp_path: Path) -> Path:
    """两条路径、**期次不同**：主库旧（但更权威），dev 库新。

    方向是刻意的：只有"最新期次"这一条判据能让 dev 库胜出 ——
    权威层级（主库 0 < dev 2）与扫描顺序都指向主库。
    这样"关掉择优"的变异一定会改落点（自证才有意义）。
    """
    _write_db(tmp_path / "data" / "moss_finagent.db", "quant_daily_basic",
              "trade_date TEXT, code TEXT, dv_ratio REAL",
              [("20230101", "600036", 1.11)])
    _write_db(tmp_path / "data" / "dev" / "moss_dev.db", "quant_daily_basic",
              "trade_date TEXT, code TEXT, dv_ratio REAL",
              [("20260924", "600036", 9.99)])
    return tmp_path


@pytest.fixture()
def paths_idx(two_paths: Path) -> ColumnIndex:
    return ColumnIndex(project_root=two_paths).build()


def _newest_wins_assertions(idx: ColumnIndex) -> None:
    """「期次更新的那张胜出」的**全部**断言（第③条与自证共用同一段）。

    抽成函数是为了让自证**真的**复用第③条的判据 ——
    自证里重写一遍断言，就成了"证明另一条判据会红"，等于没证。
    """
    paths = idx.column_paths("dv_ratio")
    assert len(paths) == 2, f"候选清单只列出 {len(paths)} 条（应列出全部路径）"
    top = paths[0]
    assert top.db == "moss_dev.db", (
        f"选中了 {top.db}（期次 {top.latest_time}）—— 应取期次更新的那张")
    assert top.reason == PathReason.NEWEST_PERIOD, (
        f"原因码是 {top.reason}；本期次可比时必须是 newest_period")
    assert top.period is not None and top.period[:3] == (2026, 9, 24)

    best = idx.best_for_column("dv_ratio")
    assert best is not None and best.db_name == "moss_dev.db"

    ex = LocalDataExecutor(index=idx)
    table, column, _cands = ex.resolve("dv_ratio")
    assert table is not None and table.db_name == "moss_dev.db", (
        "执行器没走同一次排序 —— 两套排序已经漂移")
    assert column == "dv_ratio"
    res = ex.metric_series("dv_ratio", entity="600036")
    assert res.ok, res.diag.render() if res.diag else "取数失败"
    assert res.plan["table"] == "quant_daily_basic"
    assert res.plan["db"] == "moss_dev.db"
    assert res.points[-1]["value"] == 9.99, "取到的不是期次更新的那张表的值"


# ============================================================
# ① 单一事实源（AST 断言"字面量只有一处"，不是值相等）
# ============================================================


def _candidate_literals() -> list[tuple[str, int, str, tuple[str, ...]]]:
    """三个 ``.py`` 里"候选常量字面量"的定义处（名字 → 值）。

    ⚠️ 必须同时认 ``X = (...)`` 与 ``X: tuple[str, ...] = (...)``：
    本仓库的常量带类型注解，那是 ``ast.AnnAssign``，只认 ``ast.Assign``
    会把定义全漏掉、报出"0 处定义"的**假绿**（本轮实测踩过）。
    """
    out: list[tuple[str, int, str, tuple[str, ...]]] = []
    for name in OWNED_FILES:
        tree = ast.parse((CATALOG_DIR / name).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AnnAssign):
                value, targets = node.value, [node.target]
            elif isinstance(node, ast.Assign):
                value, targets = node.value, list(node.targets)
            else:
                continue
            if not isinstance(value, (ast.Tuple, ast.List)):
                continue
            els = tuple(e.value for e in value.elts
                        if isinstance(e, ast.Constant)
                        and isinstance(e.value, str))
            if len(els) < 5 or len(els) != len(value.elts):
                continue
            if _ENTITY_MARK in els or any(m in els for m in _TIME_MARKS):
                out.append((name, node.lineno,
                            ",".join(getattr(t, "id", "?") for t in targets),
                            els))
    return out


def test_entity_and_time_candidates_have_single_source() -> None:
    """★★★ 实体/时间列候选**只有一处定义**，其余调用方一律 import。

    ## 为什么必须是"字面量唯一"而不是"值相等"

    值相等是**当下**相等；两份拷贝的注释会互相背书（"与 X 同源"），
    值却各走各的 —— 本项目实测：三份时间列候选逐字不同，
    而其中一份的注释正写着"同源同序"。

    ## 合并的判据（不是"看着像就合"）

    两个调用方（本模块选库排序 / `assets` 资产登记）扫的是**同一份 registry**
    （`data_stores.all_stores()`），问的是同一个问题（这张表的时间列是哪一列）。
    所以正确的做法是把两边的**独有项都收进来**（并集），
    而不是挑一份覆盖另一份 —— 下面把这一条也钉住。
    """
    literals = _candidate_literals()
    assert len(literals) == 2, (
        "候选常量的**字面量**定义应为 2 处（实体 1 + 时间 1），实际："
        f"{[(n, ln, t) for n, ln, t, _ in literals]} —— "
        "多出来的每一处都是一份会漂的拷贝"
    )
    files = {n for n, _ln, _t, _v in literals}
    assert files == {"column_index.py"}, (
        f"候选常量的定义跑到了 {files}；唯一事实源必须在 column_index.py")
    names = {t for _n, _ln, t, _v in literals}
    assert names == {"ENTITY_COLUMN_CANDIDATES", "TIME_COLUMN_CANDIDATES"}, names

    # 并集完整性：两侧的独有项都必须在（防止"合并"变成"挑一份"）
    for only_index in ("date", "datetime", "timestamp"):
        assert only_index in TIME_COLUMN_CANDIDATES, (
            f"{only_index!r} 只有 column_index 那一份有 —— 合并时漏了")
    assert "latest_publish_time" in TIME_COLUMN_CANDIDATES, (
        "latest_publish_time 只有 assets 那一份有（真实列：news_cache）—— 合并时漏了")

    # 运行期身份：任何还留在别处的名字都必须是**同一个对象**，不是等值拷贝
    from src.infrastructure.catalog import assets, local_data

    assert assets._TIME_COLUMN_CANDIDATES is TIME_COLUMN_CANDIDATES, (
        "assets 手里不是同一个对象 —— 又是拷贝（值相等不算通过）")
    allowed = (ENTITY_COLUMN_CANDIDATES, TIME_COLUMN_CANDIDATES)
    for mod, attr in ((local_data, "_ENTITY_COLS"), (local_data, "_TIME_COLS"),
                      (assets, "_ENTITY_COLS"), (assets, "_TIME_COLS")):
        legacy = getattr(mod, attr, None)
        assert legacy is None or any(legacy is a for a in allowed), (
            f"{mod.__name__}.{attr} 不是唯一事实源的那个对象 —— 拷贝又回来了")


# ============================================================
# ② 多路径可枚举（必须列出两条，不是只返回一条）
# ============================================================


def test_multi_path_candidates_are_enumerable(paths_idx: ColumnIndex) -> None:
    """★★ 同一列被两张表提供 ⇒ 候选清单必须**两条都列**出来。

    这是"多路径"这件事从**不可见**变成**可见**的最小判据：
    只要它还只返回一条，就没人能回答"为什么不是那张"。
    """
    hits = paths_idx.find_column("dv_ratio")
    assert len({h.asset_id for h in hits}) == 2, "夹具没造出两条路径"

    paths = paths_idx.column_paths("dv_ratio")
    assert len(paths) == 2, f"候选清单只列出 {len(paths)} 条"
    assert {p.asset_id for p in paths} == {h.asset_id for h in hits}
    assert [p.rank for p in paths] == [0, 1], "rank 必须给出先后（否则无法解释取舍）"
    assert [p.chosen for p in paths] == [True, False], "必须且只能标出一条选中"
    assert paths[0].reason and not paths[1].reason, "原因只挂在选中的那条上"

    # 登记视图与候选清单必须是**同一次排序**（防两套排序漂移）
    reg = paths_idx.dataset_registry()["dv_ratio"]
    assert [r["asset_id"] for r in reg] == [p.asset_id for p in paths]


# ============================================================
# ③ newest-wins
# ============================================================


def test_newest_period_wins(paths_idx: ColumnIndex) -> None:
    """★★★ 两条路径期次不同 ⇒ 取**期次更新**的那张（即使它更不"权威"）。"""
    _newest_wins_assertions(paths_idx)


def test_same_day_time_of_day_counts_as_newer(tmp_path: Path) -> None:
    """★ 同一天里"带时分秒"的期次比"只有日期"的更新。

    ## 为什么单列一条（实测判错方向）

    松散键（数字倒序字典序）把 `20260930` 与 `2026-09-30T21:05:43` 相比时，
    前者是后者的**前缀** → 前缀更小 → **当天 00:00 胜出**，方向反了。
    真实库实测：这一处方向错误会让 `ts_code` 的 rank-0 从
    `warehouse.db:quant_adj_factor`（15,993,661 行）变成
    `moss_finagent.db:map_stock_concept` —— 这是**修正**，不是回归。
    """
    assert period_key("20260930") == (2026, 9, 30, 0, 0, 0)
    assert period_key("2026-09-30T21:05:43") == (2026, 9, 30, 21, 5, 43)
    assert period_key("2026-09-30T21:05:43") > period_key("20260930")

    # ⚠️ 2026-10-01 裁定：时间列分**期次列**与**运维列**，只有期次列参与期次比较
    #    （`column_index.PERIOD_COLUMNS`）。所以这里用 `publish_time`（期次列）
    #    来表达"同一天里带时分秒的更新"，而不是 `updated_at`（运维列）——
    #    后者会让"写得最勤的小表"永远抢走大表的列（见那份裁定的实测证据）。
    _write_db(tmp_path / "data" / "moss_finagent.db", "t_date_only",
              "trade_date TEXT, dv_ratio REAL", [("20260930", 1.0)])
    _write_db(tmp_path / "data" / "dev" / "moss_dev.db", "t_with_time",
              "publish_time TEXT, dv_ratio REAL",
              [("2026-09-30T21:05:43", 2.0)])
    idx = ColumnIndex(project_root=tmp_path).build()
    top = idx.column_paths("dv_ratio")[0]
    assert top.table == "t_with_time", (
        f"选了 {top.table}（期次 {top.latest_time}）—— "
        "同一天里'只有日期'被判成更新，方向反了")


# ============================================================
# ④ 期次不可得：退回既有顺序、不报错、不猜
# ============================================================


@pytest.mark.parametrize("shape,ddl,row", [
    ("没有时间列", "dv_ratio REAL, code TEXT", (1.11, "600036")),
    ("时间列是垃圾值", "trade_date TEXT, code TEXT, dv_ratio REAL",
     ("--", "600036", 1.11)),
    ("时间列是 epoch 毫秒", "trade_date TEXT, code TEXT, dv_ratio REAL",
     ("1790842694098", "600036", 1.11)),
])
def test_period_unavailable_falls_back_to_existing_order(
    tmp_path: Path, shape: str, ddl: str, row: tuple,
) -> None:
    """★★ 期次取不到 ⇒ 退回**既有顺序**（权威 → 行数），不报错、**不猜**。

    ## 三种"期次不可得"的形状都要覆盖

    · 没有时间列（`latest_time` 为空）
    · 时间列存的是垃圾值（`MAX()` 拿到 `--`）
    · 时间列存的是 epoch 毫秒（实测 `indicator_catalog` 就是 `1790842694098`）
      —— 单位（s/ms）与时区**都无法在本地断言**，所以一律不肯猜

    判据是"行为确定"：无论哪种形状，落点都必须与**期次参与之前**的既有顺序一致
    （主库 0 < dev 2 → 主库），并且原因码必须**说出**"这次没比期次"。
    """
    _write_db(tmp_path / "data" / "moss_finagent.db", "quant_x", ddl, [row])
    _write_db(tmp_path / "data" / "dev" / "moss_dev.db", "quant_x", ddl, [row])
    idx = ColumnIndex(project_root=tmp_path).build()

    paths = idx.column_paths("dv_ratio")
    assert len(paths) == 2, f"{shape}：两条路径没被列全"
    top = paths[0]
    assert top.period is None, f"{shape}：期次竟然被解析出来了（那就是在猜）"
    assert top.db == "moss_finagent.db", (
        f"{shape}：落点 {top.db}，既有的权威顺序（主库优先）没被保留")
    assert top.reason == PathReason.PERIOD_UNAVAILABLE, (
        f"{shape}：原因码是 {top.reason}；期次不可得必须**说出来**，"
        "否则日志读起来像「期次已经比过了」——那是我们在猜")
    assert "epoch" in PATH_REASON_TEXT[PathReason.PERIOD_UNAVAILABLE] or (
        "退回" in PATH_REASON_TEXT[PathReason.PERIOD_UNAVAILABLE])

    # 不报错：取数照常返回，且把"为什么"带在 plan 里
    ex = LocalDataExecutor(index=idx)
    res = ex.metric_series("dv_ratio", entity="600036")
    assert res.ok, f"{shape}：退回既有顺序后取不到数（应照常返回）"
    assert res.plan["db"] == "moss_finagent.db"
    assert res.plan["path_reason"] == PathReason.PERIOD_UNAVAILABLE
    assert res.plan["paths"] == 2


# ============================================================
# ⑤ 自证：把择优关掉，第③条必须变红
# ============================================================


def test_selfproof_disabling_newest_period_would_go_red(
    two_paths: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★★ **自证**：关掉"最新期次"这条判据后，第③条必须红。

    ## 为什么判据要带自证

    "择优生效了"这句话有两种假绿：①判据其实没走到择优那条分支；
    ②`pytest.raises` 因为别的原因通过。所以本用例做两件事：

      1. **先断言变异真的改了落点**（否则后面那个 `raises` 毫无意义）
      2. 再把第③条的**同一段断言**放进 `raises` 里跑

    变异方式：把 `_rank_key` 换成只保留"有没有数据"——
    排序退化成**扫描顺序**（主库在前），也就是"只取第一张"。
    """
    monkeypatch.setattr(ci, "_rank_key", lambda t: (not t.has_data,))
    idx = ColumnIndex(project_root=two_paths).build()

    mutated_top = idx.column_paths("dv_ratio")[0]
    assert mutated_top.db == "moss_finagent.db", (
        "变异没生效（落点没变）→ 后面的 raises 不能算自证；"
        "检查 _rank_key 是否仍是模块级、被 _ranked_tables 按名字调用")

    with pytest.raises(AssertionError):
        _newest_wins_assertions(idx)


# ============================================================
# ⑥ 防漂移：三条路径共享一次排序
# ============================================================


def test_column_paths_and_best_for_column_share_one_ranking(
    paths_idx: ColumnIndex,
) -> None:
    """★ 候选清单 / `best_path` / `best_for_column` / 执行器必须**同一个落点**。

    "两套排序必然漂移"是本项目已登记的缺陷类别。这里断言的是**行为等价**
    （同一个 asset_id），不是源码里有没有某个字样 —— 后者在单进程多模块加载时假红。
    """
    col = "dv_ratio"
    paths = paths_idx.column_paths(col)
    best_path = paths_idx.best_path(col)
    best_table = paths_idx.best_for_column(col)
    reg_top = paths_idx.dataset_registry()[col][0]
    table, column, _c = LocalDataExecutor(index=paths_idx).resolve(col)

    ids = {
        paths[0].asset_id,
        None if best_path is None else best_path.asset_id,
        None if best_table is None else best_table.asset_id,
        reg_top["asset_id"],
        None if table is None else table.asset_id,
    }
    assert len(ids) == 1, f"同一列选出了不同的表：{ids}"
    assert column == col


def test_unknown_column_has_no_paths(paths_idx: ColumnIndex) -> None:
    """查不到的列：候选为空、`best_path` 为 `None`（**没量到就是没量到**）。"""
    assert paths_idx.column_paths("no_such_column_xyz") == []
    assert paths_idx.best_path("no_such_column_xyz") is None
    assert paths_idx.best_for_column("no_such_column_xyz") is None


# ============================================================
# ⑦ 判据表本身：排序键分量与原因码必须一一对得上
# ============================================================


def test_rank_criteria_labels_match_key_arity() -> None:
    """★ 排序键的**分量个数**必须等于原因码的表长。

    为什么：原因码是"逐分量比较排序键"得出来的。若给排序键加一条判据却忘了
    登记名字，解释就会**默默少一层**（落到 `else` 分支上）——
    那时"为什么选它"开始说谎，而没有任何红灯。
    """
    sample = ci.TableColumns(db_path="x.db", table="t")
    key = ci._rank_key(sample)
    assert len(ci._RANK_CRITERIA) == len(key), (
        f"排序键有 {len(key)} 个分量，判据名只有 {len(ci._RANK_CRITERIA)} 个")
    assert ci._RANK_CRITERIA[:2] == ("has_data", "period"), (
        "前两条判据必须是『有没有数据』与『期次』—— 顺序变了，择优规则就变了")


def test_every_path_reason_is_documented() -> None:
    """★ 每个原因码都要有"一句人话"（风格同 `local_data.DiagCode`）。

    ⚠️ 判据**不写死总数**：新增原因码时只要登记进 `PATH_REASON_TEXT` 即可
    （写死数字会在合理扩展时假红，本项目踩过）。
    """
    codes = {v for k, v in vars(PathReason).items() if not k.startswith("_")}
    assert codes, "PathReason 里一个码都没有"
    missing = codes - set(PATH_REASON_TEXT)
    assert not missing, f"以下原因码没有说明：{missing}"
    for code in codes:
        assert PATH_REASON_TEXT[code].strip(), f"{code} 的说明是空串"


# ============================================================
# ⑧ 可见性：结果里必须能读出"几条路径 / 选了哪条 / 为什么"
# ============================================================


def test_result_plan_exposes_path_visibility(paths_idx: ColumnIndex) -> None:
    """★★ 「这一列有几条路径、选了哪条、为什么」必须**在结果里读得到**。

    以前这些信息只存在于"读代码的人脑子里"：返回体里只有 `db`/`table`，
    没有路径条数、没有原因 —— 于是"为什么不是那张表"永远答不出来。
    """
    res = LocalDataExecutor(index=paths_idx).metric_series(
        "dv_ratio", entity="600036")
    assert res.ok
    assert res.plan["paths"] == 2
    assert res.plan["path_rank"] == 0
    assert res.plan["path_reason"] == PathReason.NEWEST_PERIOD
    assert "期次" in res.plan["path_why"], res.plan["path_why"]
    cands = res.plan["path_candidates"]
    assert len(cands) == 2 and ":" in cands[0], f"候选路径串不对：{cands}"
    assert cands[0].startswith("moss_dev.db:"), (
        f"候选清单第一条不是被选中的那张：{cands}")


def test_single_path_column_says_so(two_paths: Path) -> None:
    """反向断言：只有一条路径时原因码是 `single_path`（没有取舍就别装作有）。"""
    _write_db(two_paths / "data" / "moss_finagent.db", "solo",
              "trade_date TEXT, only_here REAL", [("20260924", 1.0)])
    idx = ColumnIndex(project_root=two_paths).build()
    paths = idx.column_paths("only_here")
    assert len(paths) == 1
    assert paths[0].reason == PathReason.SINGLE
    res = LocalDataExecutor(index=idx).metric_series("only_here")
    assert res.ok and res.plan["paths"] == 1
    assert res.plan["path_reason"] == PathReason.SINGLE


# ============================================================
# ⑨ 同一个文件被多个存储条目指向：只算**一条**路径
# ============================================================


def test_same_file_registered_by_many_stores_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 多个存储条目指向**同一个文件**时，只能算一条路径。

    ## 为什么这条决定"路径数"是否诚实（实测）

    registry 里有 3 个 sqlite 条目（`app_db` / `legacy_main` / `crowding_shared`）
    指向同一个 `data/moss_finagent.db`。按条目逐个收下 → 该库每张表被扫 3 次
    （191 行里 48 个 `asset_id` 重复）→ "多路径列"被虚报成 **345**，
    **去重后只有 119**。

    多路径择优的全部价值就压在这个数字上：虚报 3 倍之后，
    "这一列有几条路径"变成一句没人复核得过来的假话。
    """
    from src.infrastructure.catalog import data_stores as ds

    db = tmp_path / "same.db"
    _write_db(db, "quant_y", "trade_date TEXT, dv_ratio REAL",
              [("20260924", 1.0)])
    stores = (ds.Store(name="app_db", kind="sqlite", path=str(db)),
              ds.Store(name="legacy_main", kind="sqlite", path=str(db)))
    monkeypatch.setattr(ds, "all_stores", lambda: stores)
    monkeypatch.setattr(ds, "unregistered_databases", lambda *a, **k: [])

    idx = ColumnIndex().build()      # 默认 project_root = 仓库根 → 走 registry 分支
    assert len(idx.discover_databases()) == 1, (
        "同一个文件被两个存储条目指向，却收下了两次 → 路径数会虚报")
    hits = [t for t in idx.tables if t.table == "quant_y"]
    assert len(hits) == 1, f"同一张表进了索引 {len(hits)} 次"
    paths = idx.column_paths("dv_ratio")
    assert len(paths) == 1, f"同一张表被算成 {len(paths)} 条路径"
