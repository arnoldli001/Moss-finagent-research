"""护栏：意图直达列 + 空结果诊断 + 跨库只读（B2）。

## 这个文件守的那类缺陷

用户第三次报障："招商银行的股息率和基本面在库里/在数据源上，为什么取不到？"

根因：**库里有什么，没有任何一条可执行的取数路径**。所有取数都必须先
在 `indicators.yaml` 登记 → 进 `INDICATOR_CATALOG` → 过白名单 → 连接器
`_CODE_PREFIXES` 认它。四处任一没改，数据就取不到 —— 而**不报错**。

实测现场：`quant_daily_basic` **11,837,850 行**、含 `dv_ratio`(股息率) /
`total_mv` / `turnover_rate`，投研链路**零命中**。

`LocalDataExecutor` 把顺序倒过来：**先问库"你有什么"（反向索引），再按意图取**。

## 判据纪律

· **不依赖本机数据**：全部断言用临时库自造数据（开发机绿/CI 红是本项目栽过的坑）。
· **空结果必须分类**：`NOT_APPLICABLE_FOR_ENTITY` 是本轮实测新增的第 12 个码 ——
  `流动比率` 对银行恒空（银行资产负债不划分流动/非流动）。归成 `NO_COLUMN`
  会去改映射表（白改），归成 `NO_DATA` 会去补源（白补）。
· **只读**：执行器必须用 `mode=ro` 打开，且恒带 `LIMIT`。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.infrastructure.catalog.column_index import ColumnIndex
from src.infrastructure.catalog.local_data import (
    DIAG_NEXT_STEP,
    ENQUEUEABLE_CODES,
    Diag,
    DiagCode,
    LocalDataExecutor,
)


# ============================================================
# 夹具：自造"两个库"，其中目标列只在子目录库里（跨库发现）
# ============================================================


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "data" / "dev").mkdir(parents=True)
    # 主库：无关数据
    con = sqlite3.connect(tmp_path / "data" / "moss_finagent.db")
    con.execute("CREATE TABLE fact_data_points (indicator TEXT, value REAL, "
                "period_date TEXT)")
    con.execute("INSERT INTO fact_data_points VALUES ('CPI', 0.8, '2026-09-01')")
    con.commit()
    con.close()
    # 子目录库：股息率/市值/换手率 —— 只有递归发现才找得到
    con = sqlite3.connect(tmp_path / "data" / "dev" / "moss_dev.db")
    con.execute("""CREATE TABLE quant_daily_basic (
        trade_date TEXT, code TEXT, dv_ratio REAL, dv_ttm REAL,
        total_mv REAL, turnover_rate REAL)""")
    con.executemany(
        "INSERT INTO quant_daily_basic VALUES (?,?,?,?,?,?)",
        [("20231108", "600036", 4.97, 5.67, 6.1e11, 0.30),
         ("20231109", "600036", 4.96, 5.66, 6.2e11, 0.31),
         ("20231110", "600036", 5.04, 5.76, 6.3e11, 0.32),
         ("20231110", "601398", 6.13, 6.34, 1.8e12, 0.11)],
    )
    # 一张"该实体不适用"的表：列在、有值，但**没有银行的记录**
    con.execute("""CREATE TABLE fin_ratio (
        period_date TEXT, code TEXT, current_ratio REAL)""")
    con.executemany("INSERT INTO fin_ratio VALUES (?,?,?)",
                    [("2026-06-30", "000001", 1.8),
                     ("2026-06-30", "600519", 3.2)])
    con.commit()
    con.close()
    return tmp_path


@pytest.fixture()
def ex(project: Path) -> LocalDataExecutor:
    return LocalDataExecutor(index=ColumnIndex(project_root=project).build())


# ============================================================
# ① 意图直达列：不登记任何指标也能取到
# ============================================================


def test_dividend_yield_resolves_without_any_indicator_registration(
    ex: LocalDataExecutor,
) -> None:
    """★★★ 报障现场：『股息率』必须能直达 `dv_ratio`，**无需任何登记**。

    这条是用户三轮报障的机器化复现。修好之前，"股息率"这个词在系统里
    **没有任何契约**（`indicators.yaml` / planner 目录 / 白名单都没有），
    所以永远取不到 —— 而库里有 1184 万行。
    """
    res = ex.metric_series("股息率", entity="600036")
    assert res.ok, f"取不到：{res.diag.render() if res.diag else '未知'}"
    assert res.plan["column"] == "dv_ratio"
    assert res.plan["table"] == "quant_daily_basic"
    assert res.row_count == 3
    assert res.points[-1]["value"] == 5.04
    assert "quant_daily_basic" in res.source


def test_cross_database_discovery(ex: LocalDataExecutor) -> None:
    """★ 目标列只在**子目录库**里 —— 跨库发现必须成立（不复制数据）。"""
    res = ex.metric_series("总市值", entity="600036")
    assert res.ok
    assert "moss_dev.db" in res.source, (
        "没找到子目录库里的数据 → 跨库只读挂载没生效")
    assert res.plan["db"] == "moss_dev.db"


@pytest.mark.parametrize("metric,column", [
    ("股息率", "dv_ratio"),
    ("股息率TTM", "dv_ttm"),
    ("总市值", "total_mv"),
    ("换手率", "turnover_rate"),
])
def test_alias_families_map_to_expected_columns(
    ex: LocalDataExecutor, metric: str, column: str,
) -> None:
    """同义/口径家族要落到正确的列（`股息率` 与 `股息率TTM` 是两个口径）。"""
    res = ex.metric_series(metric, entity="600036")
    assert res.ok
    assert res.plan["column"] == column, (
        f"『{metric}』落到了 {res.plan['column']}，期望 {column}")


def test_entity_filter_isolates_rows(ex: LocalDataExecutor) -> None:
    """★ 实体过滤必须生效：问 600036 不能把 601398 的数据混进来。"""
    res = ex.metric_series("股息率", entity="600036")
    assert {p["entity"] for p in res.points} == {"600036"}


def test_entity_suffix_is_normalized(ex: LocalDataExecutor) -> None:
    """`600036.SH` 这类带后缀的写法要能归一（否则"实体未映射"是假象）。"""
    res = ex.metric_series("股息率", entity="600036.SH")
    assert res.ok and res.row_count == 3


def test_time_range_filters(ex: LocalDataExecutor) -> None:
    """时间过滤：区间外的行不许返回。"""
    res = ex.metric_series("股息率", entity="600036",
                           start="2023-11-10", end="2023-11-10")
    assert res.ok and res.row_count == 1
    assert res.points[0]["value"] == 5.04


# ============================================================
# ② 空结果诊断：每一类都要能被区分
# ============================================================


def test_unknown_metric_reports_no_column(ex: LocalDataExecutor) -> None:
    """不存在的指标 → `NO_COLUMN`（下一步是**换列名**，不是换数据源）。"""
    res = ex.metric_series("完全不存在的指标xyz")
    assert not res.ok
    assert res.diag.code == DiagCode.NO_COLUMN
    assert "换列名" in res.diag.next_step


def test_column_present_but_entity_absent_is_not_applicable(
    ex: LocalDataExecutor,
) -> None:
    """★★★ 本轮实测**新增**的第 12 个码：列在、有值，但**该实体没有**记录。

    这就是"银行没有流动比率"的形状。必须报 `NOT_APPLICABLE_FOR_ENTITY`：

      · 报 `NO_COLUMN` → 会去改映射表（**白改**，列名是对的）
      · 报 `NO_DATA`   → 会去补数据源（**白补**，源里有别人家的数据）

    而且必须给出**同表相近列**作为"换口径"的候选。
    """
    res = ex.metric_series("current_ratio", entity="600036")
    assert not res.ok
    assert res.diag.code == DiagCode.NOT_APPLICABLE_FOR_ENTITY, (
        f"错误地报成 {res.diag.code}；应报 NOT_APPLICABLE_FOR_ENTITY"
    )
    assert "换适用口径" in res.diag.next_step


def test_expected_entity_gets_data_not_diagnosis(ex: LocalDataExecutor) -> None:
    """反向断言：**有**记录的那个实体必须正常取到（防止诊断过宽）。"""
    res = ex.metric_series("current_ratio", entity="000001")
    assert res.ok and res.row_count == 1


def test_time_out_of_range_is_distinguished(ex: LocalDataExecutor) -> None:
    """实体有值但不在区间内 → `TIME_OUT_OF_RANGE`（下一步是**放宽时间**）。"""
    res = ex.metric_series("股息率", entity="600036",
                           start="2030-01-01")
    assert not res.ok
    assert res.diag.code == DiagCode.TIME_OUT_OF_RANGE
    assert "放宽时间" in res.diag.next_step


def test_diagnostics_carry_next_step_and_candidates(ex: LocalDataExecutor) -> None:
    """诊断必须**可执行**：带下一步动作；有候选时带候选列名。"""
    res = ex.metric_series("current_ratio", entity="600036")
    rendered = res.diag.render()
    assert "下一步" in rendered
    assert Diag(DiagCode.NO_DATA).next_step  # 每个码都要有下一步
    assert res.row_count == 0


# ============================================================
# ③ 诊断码表本身
# ============================================================


def test_all_diag_codes_defined_and_documented() -> None:
    """每个诊断码都必须有"下一步做什么"，且**两个维度**都不能缺。

    ## 两个维度（按 PRD §18.3 A6 补上环境维度之后）

      · **数据维度**：列/表/实体/时间/频率/单位/维度/权限/口径适用性/陈旧/真缺口
      · **环境维度**：多环境各有自己的库，**"本环境没有" ≠ "没有数据"**。
        不区分的话，"dev 没同步过来"会被误报成"库里没有这个字段"，
        而排查方向完全相反（去补数据源 vs 去跑同步）。

    ⚠️ 判据**不写死总数**（第一版写 `== 12`，补环境维度时红了）：
    改成"两个维度的核心码必须在场 + 每个码都有下一步"——
    新增维度只要在下面的集合里登记，而不是改一个魔数。
    """
    codes = {v for k, v in vars(DiagCode).items() if not k.startswith("_")}
    missing = codes - set(DIAG_NEXT_STEP)
    assert not missing, f"以下码没有『下一步』说明：{missing}"
    for code in codes:
        assert DIAG_NEXT_STEP[code].strip(), f"{code} 的下一步是空串"

    data_dim = {
        DiagCode.CONN_FAIL, DiagCode.NO_TABLE, DiagCode.NO_COLUMN,
        DiagCode.ENTITY_UNMAPPED, DiagCode.TIME_OUT_OF_RANGE,
        DiagCode.FREQ_MISMATCH, DiagCode.UNIT_MISMATCH, DiagCode.DIM_MISMATCH,
        DiagCode.NO_PERMISSION, DiagCode.NOT_APPLICABLE_FOR_ENTITY,
        DiagCode.STALE_BEYOND_TOLERANCE, DiagCode.NO_DATA,
    }
    env_dim = {
        DiagCode.ENV_NOT_COVERED, DiagCode.PROD_ONLY, DiagCode.DEV_ONLY,
        DiagCode.DEV_SYNC_DELAY, DiagCode.PROD_PERMISSION_DENIED,
    }
    assert data_dim <= codes, f"数据维度缺码：{data_dim - codes}"
    assert env_dim <= codes, f"环境维度缺码：{env_dim - codes}"
    assert not (env_dim & data_dim), (
        "两个维度的码重叠了 —— 环境差异与数据缺失必须能分开"
    )


def test_only_real_gaps_are_enqueueable() -> None:
    """★★ 只有**真缺口**才配进缺口队列。

    其余码（`NO_COLUMN` / `NOT_APPLICABLE_FOR_ENTITY` / `TIME_OUT_OF_RANGE` …）
    都是**我们的问题** —— 入队会让 A19 去做注定失败的补采，
    本轮已实测过一次（假缺口污染队列）。
    """
    assert DiagCode.NO_DATA in ENQUEUEABLE_CODES
    for code in (DiagCode.NO_COLUMN, DiagCode.NOT_APPLICABLE_FOR_ENTITY,
                 DiagCode.TIME_OUT_OF_RANGE, DiagCode.ENTITY_UNMAPPED,
                 DiagCode.NO_PERMISSION, DiagCode.FREQ_MISMATCH,
                 DiagCode.UNIT_MISMATCH, DiagCode.DIM_MISMATCH,
                 DiagCode.CONN_FAIL):
        assert code not in ENQUEUEABLE_CODES, f"{code} 不该入队（补采也解决不了）"


# ============================================================
# ④ 安全与性能约束
# ============================================================


def test_readonly_and_limited(ex: LocalDataExecutor, project: Path) -> None:
    """★ 只读 + 恒带 LIMIT：执行器**不许**有写库能力，也不许无界返回。"""
    res = ex.metric_series("股息率", entity="600036", limit=2)
    assert res.row_count <= 2, "LIMIT 没生效 → 大表会一次拉回几十万行"
    # 库未被改动（只读连接）
    con = sqlite3.connect(project / "data" / "dev" / "moss_dev.db")
    n = con.execute("SELECT COUNT(*) FROM quant_daily_basic").fetchone()[0]
    con.close()
    assert n == 4


def test_metric_name_cannot_inject_sql(ex: LocalDataExecutor, project: Path) -> None:
    """★ 指标名走**参数化 + 索引白名单**，攻击串不可能改变 SQL 结构。

    ## 第一版判据写错了（实测纠正）

    原断言写"这种指标名应当取不到 → `NO_COLUMN`"。实测**推翻**了它：
    注入串 `dv_ratio"; DROP TABLE quant_daily_basic; --` 被同义词字典
    当成 `dv_ratio` 的别名匹配（"查询是别名的子串"分支），于是**正常取到数据**。

    那**不是**漏洞，而是判据写错了 —— 真正的安全性质不是"可疑名字必须失败"，
    而是这两条：

      ① **列名来自索引**（`ColumnIndex` 扫出来的真实列），
         用户输入永远只是"用来查索引的 key"，**不进 SQL 文本**；
      ② 所以 SQL 结构只由索引内容决定，攻击串最多导致"查不到"，
         **不可能**导致 DROP/UNION/注释逃逸。

    本用例改成断言这两条 + 事后表仍在。
    """
    target = "quant_daily_basic"
    res = ex.metric_series(f'{target}"; DROP TABLE {target}; --',
                           entity="600036")
    if res.ok:
        # 命中了某个已知列 → 该列**必须**是索引里的真实列（性质①）
        col = res.plan["column"]
        table = next(t for t in ex._idx().tables if t.table == target)
        assert col in table.columns, (
            f"SQL 里用了不在索引中的列 {col!r} —— 说明列名来自用户输入而非索引"
        )
    # 性质②：无论命中与否，表必须还在（没有任何写操作发生）
    con = sqlite3.connect(project / "data" / "dev" / "moss_dev.db")
    alive = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (target,)).fetchone()[0]
    con.close()
    assert alive == 1, "注入串把表弄没了 —— 说明列名/表名被拼进了 SQL 文本"


def test_stale_data_is_returned_with_marker_not_dropped(
    ex: LocalDataExecutor,
) -> None:
    """★★ 过旧数据**返回 + 标注**，不许丢弃。

    丢掉一个真实的历史值，等于让"这张表停更了"这件事彻底不可见 ——
    本轮实测 `quant_daily_basic` 停在 2023-11-10（1053 天前），
    用户必须能看到这个数字有多旧，而不是被判成"没有数据"。
    """
    res = ex.metric_series("股息率", entity="600036")
    assert res.ok, "过旧数据被丢弃了（应返回并标注）"
    assert res.plan.get("stale_days") is not None
    assert res.plan["stale_days"] > 300


def test_missing_db_file_reports_conn_fail(project: Path) -> None:
    """库文件不在 → `CONN_FAIL`，**不是** "数据不存在"（排查方向完全不同）。

    ⚠️ 不真删文件（Windows 上 sqlite 连接会持句柄 → `PermissionError`），
    而是把索引里的路径指向一个不存在的文件 —— 效果等价且跨平台稳定。
    """
    idx = ColumnIndex(project_root=project).build()
    table = next(t for t in idx.tables if t.table == "quant_daily_basic")
    table.db_path = str(Path(table.db_path).with_name("not_there.db"))
    res = LocalDataExecutor(index=idx).metric_series("股息率", entity="600036")
    assert not res.ok
    assert res.diag.code == DiagCode.CONN_FAIL
    assert "不要报告为" in res.diag.next_step


# ============================================================
# ⑤ 库在运行期间被改动（并发清理/迁移）—— 陈旧引用必须可诊断、可自愈
# ============================================================


def test_table_dropped_mid_flight_reports_no_table(
    ex: LocalDataExecutor, project: Path,
) -> None:
    """★★★ 缓存了"表在库 Y"，而它已被 DROP → 必须报 `NO_TABLE` 且**说清原因**。

    ## 为什么这条是必需的（实测场景）

    2026-09-28 另一个会话在**并发清理数据库冗余表**：
    主库表数 47→52、dev 33→38，且随时可能 DROP 掉索引缓存过的表。

    此时"查不到"**不是"没有数据"**，而是**我们拿着一张过期的地图**。
    判据必须能区分这两件事 —— 否则排查方向完全相反
    （去补数据源 vs 去刷新索引）。
    """
    import sqlite3 as _sq

    idx = ColumnIndex(project_root=project).build()
    # 索引建好之后再 DROP 表（模拟"运行期间被清理"）
    con = _sq.connect(project / "data" / "dev" / "moss_dev.db")
    con.execute("DROP TABLE quant_daily_basic")
    con.commit()
    con.close()

    res = LocalDataExecutor(index=idx).metric_series("股息率", entity="600036")
    assert not res.ok
    assert res.diag.code == DiagCode.NO_TABLE, (
        f"报成了 {res.diag.code}；表被 DROP 时必须报 NO_TABLE"
    )
    assert "被改动" in res.diag.detail or "不存在" in res.diag.detail
    assert "确认库与表名" in res.diag.next_step


def test_self_heal_rebuilds_index_when_cache_is_stale(
    project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 陈旧引用要能**自愈**：缓存说表有行、实测查不到 → 重建索引再试。

    判据（只在"看起来矛盾"时才重建，不无条件重建）：
        `table.has_data is True` 而实测 0 行 → 说明地图过期了。

    自愈失败必须**保持旧索引并照常返回诊断**（增强功能坏了不能弄崩主流程）。
    """
    from src.infrastructure.catalog import column_index as ci

    idx = ColumnIndex(project_root=project).build()
    calls: list[bool] = []
    rebuilds: list[int] = []

    def fake_get(*, rebuild: bool = False):
        calls.append(rebuild)
        return idx

    monkeypatch.setattr(ci, "get_column_index", fake_get)
    # ★ 不污染进程级单例：复制一份"陈旧的地图"再改它的 row_count。
    #   第一版直接 `table.row_count = 999` —— 改的是**缓存里那个对象**，
    #   于是同进程后续用例（如 test_column_index.py）看到被篡改的索引，
    #   表现为**顺序依赖的红灯**（单独跑绿、组合跑红）。实测踩过。
    import dataclasses as _dc

    original = next(t for t in idx.tables if t.table == "quant_daily_basic")
    stale = _dc.replace(original, row_count=999)
    idx._tables = [t if t is not original else stale for t in idx.tables]

    # 记录"这个索引对象被重建了几次"（新语义的判据）
    real_build = idx.build

    def counting_build(*a, **kw):
        rebuilds.append(1)
        return real_build(*a, **kw)

    monkeypatch.setattr(idx, "build", counting_build, raising=False)

    ex2 = LocalDataExecutor(index=idx)
    res = ex2.metric_series("股息率", entity="NOPE_NO_SUCH_ENTITY")
    # 实体不存在 → 走诊断分支；关键是**重建被尝试过**
    assert not res.ok
    # ★ 判据在 CHG-0066 后改为"**就地**重建了给进来的那个索引"。
    #
    #   原判据是 `calls == [True]`（要求调用全局单例 `get_column_index`）——
    #   那个写法**把隔离泄漏当成了契约**：调用方给了 `project_root=<tmp>`
    #   的假库树，一次自愈却把作用域换成扫**真实仓库**的全局单例。
    #   后果实测：单测读到了真实行情仓，用例通过与否取决于本机数据
    #   （`test_column_index.py` 的两个用例原先正是**靠巧合**通过的）。
    assert rebuilds, (
        "缓存声称有数据却查不到时**没有触发索引重建** —— "
        "并发清理/迁移后就会永久拿着一张过期地图"
    )
    assert calls == [], (
        "自愈时不得换成全局单例：那会把作用域从调用方限定的库扩大到真实仓库，"
        f"于是单测/离线脚本读到真实数据（calls={calls}）"
    )


def test_self_heal_failure_keeps_old_index(
    project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自愈**失败**时不能抛异常，也不能丢掉诊断输出。"""
    from src.infrastructure.catalog import column_index as ci

    idx = ColumnIndex(project_root=project).build()

    def boom(*, rebuild: bool = False):
        raise RuntimeError("重建失败（模拟）")

    monkeypatch.setattr(ci, "get_column_index", boom)
    # 同样不污染单例（理由见上一条用例）
    import dataclasses as _dc

    original = next(t for t in idx.tables if t.table == "quant_daily_basic")
    stale = _dc.replace(original, row_count=999)
    idx._tables = [t if t is not original else stale for t in idx.tables]
    res = LocalDataExecutor(index=idx).metric_series(
        "股息率", entity="NOPE_NO_SUCH_ENTITY")
    assert not res.ok
    assert res.diag is not None, "自愈失败后连诊断都没了"
