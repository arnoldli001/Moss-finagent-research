"""判据：`_quant_column_points` 空结果的**证据分流**（能分开的分开、分不开的 fail-closed）。

## 防的是什么（每条判据对应一种会静默变假的失效）

上一轮（`CHG-0157` §33.5 第 2 条）登记了这个**分不开的抛点**：一条 SQL 把
「code 不在表内 / 该列全 NULL / 区间外」折叠成 `rows == []`，而**每列"NULL 是
合法无值还是数据洞"没有登记处**（`dv_ttm` 全 NULL 可能是真没分红、`total_mv`
全 NULL 则是数据洞）⇒ 分列豁免就是**猜语义 = 多豁免**（最危险方向），故保持
fail-closed 按缺陷报。

本轮只做"能分开的那一支"：探针①（不加列过滤、不加区间）**无行** ⇒
这张表**不收录该主体** ⇒ `not_covered`。

| # | 判据（函数名） | 防的是 |
|---|---|---|
| 1 | `test_code_absent_from_table_is_structured_not_covered` | 真缺口没人认 |
| 2 | `test_unregistered_column_stays_a_defect_not_an_exemption` | 多豁免（裁定 1 的**默认方向**） |
| 3 | `test_registered_legitimate_column_is_structured_not_applicable` | 合法无值被当成缺陷（登记表白建） |
| 4 | `test_registered_hole_column_stays_a_defect` | `hole` 条目被当成豁免 |
| 5 | `test_column_has_values_but_out_of_range_stays_a_defect` | 多豁免 |
| 6 | `test_probes_run_only_on_the_empty_path` | 热路径变慢 |
| 7 | `test_router_interception_unchanged_and_marker_survives` | 拦截面破 |
| 8 | `test_table_identity_is_a_single_fact_source` | 抄第二份表名 |
| 9 | `test_etf_two_gates_are_not_swapped` | ETF 两档**互换**（覆盖问题 ↔ 口径不存在） |
| 10 | `test_probe_failure_is_a_defect_not_a_coverage_gap` | 源故障被豁免 |

逐条说清楚各自防什么：

* 1 ：「表里没这只票」仍被当成取数链故障（该豁免的真缺口没人认）；
* 2 ：「有行、该列历史全 NULL」**未登记**却被豁免掉 —— 用户 2026-10-02 裁定 1 建的
      登记表（`configs/column_null_policy.yaml`）里，**未登记 = 普通失败**是默认，
      反过来（默认豁免）就是多豁免，而多豁免会把真缺口说成"本来就没有"；
* 3 ：反向的失效 —— 已登记为**合法无值**的列（如 `dv_ttm`：没分红 ⇒ 股息率没有定义）
      仍被报成缺陷 ⇒ 登记表白建、且会把"这个口径对它没有值"这条真信息抹掉；
* 4 ：登记为 `hole` 的列（如 `total_mv`）被当成豁免 —— `hole` 的含义就是"这是缺口"，
      豁免它等于**把该修的洞说成正常**；
* 5 ：「有行、该列历史有值、只是区间内没有」被豁免掉（区间/新鲜度问题）；
* 6 ：为了观测把**热路径**变慢（非空路径上多跑查询）；查询不再来自事实源；
* 7 ：既有 `except DataFetchError` 拦截面被改坏（漏接 ⇒ 不再换源）、聚合后标记丢；
* 8 ：表名/列名/主体条件各写一份 ⇒ 改一处不红（漂移）；
* 9 ：ETF 那两档互换 —— `流动比率`（口径对 ETF **不存在** ⇒ `not_applicable`）与
      `总市值`/`股息率TTM`（口径**存在**、是我们的表不收 ETF ⇒ `not_covered`，
      用户 2026-10-02 裁定 2 原话「算**覆盖问题**」）。换错档对客户的说法正好读反；
* 10 ：探针读失败被当成"不在表里" ⇒ **数据源故障**被豁免成"未收录"。

## 自证（真改源码跑红，见 `scripts/_prove_null_policy_and_etf_coverage.py`）

* **自证 A**：把第②支的分流去掉（回到"该列历史全 NULL 一律普通失败"）⇒ 判据 3 必红；
* **自证 B**：把 ETF 那个档（`:925`）改回普通失败 ⇒ 判据 9 必红。

实测输出见交付说明。

## 纪律

全部**进程内**：临时 SQLite 库（`MOSS_QUANT_SQLITE` 指到 `tmp_dir`）+ 直接调用
真实方法，**不碰**生产的 `data/quant/warehouse.db`、不发网络请求。
结论一律问**真实分类器** `classify_gap_kind`，不只看异常类型 —— 只看类型正是
上一版的漏洞形状（文本里混进标记时它会静默豁免）。
判据不许把登记表的内容抄成字面量：**哪一列登记成什么**一律现读
`null_policy`（抄一份 ⇒ 表改了判据不跟着变 ⇒ 假绿）。
"""
from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from src.core.exceptions import DataFetchError, NoApplicableData
from src.core.schemas import DataPoint
from src.domain.agents.data.collector.logic import (
    GAP_KIND_NOT_APPLICABLE,
    GAP_KIND_NOT_COVERED,
    classify_gap_kind,
)
from src.infrastructure.connectors import null_policy
from src.infrastructure.connectors.akshare_connector import (
    _QUANT_BASIC_TABLE,
    _QUANT_COLUMN_INDICATORS,
    AkshareConnector,
)

#: 表内**有行**的标的（造出来的，不是真代码语义）。
COVERED = "600036"
#: 表内**一行都没有**的标的（"该表不收录它"）。
NOT_COVERED = "301999"

#: 取数用的列（`dv_ttm` = 股息率TTM）与它对应的指标前缀 —— **从事实源现取**，
#: 不抄字面量：`_QUANT_COLUMN_INDICATORS` 改了列名，判据跟着走。
COLUMN = _QUANT_COLUMN_INDICATORS["股息率TTM"]
INDICATOR = f"股息率TTM:{COVERED}"

#: 主查询与探针共用的表结构（列形状照真实表：`trade_date`/`code` 之外是估值列）。
_COLUMNS: tuple[str, ...] = ("trade_date", "code", *_QUANT_COLUMN_INDICATORS.values())

#: 指标前缀 → 列名（判据要按列反查前缀，用来拼出**真实形状**的 indicator）。
_PREFIX_OF: dict[str, str] = {c: p for p, c in _QUANT_COLUMN_INDICATORS.items()}


def _all_null_code(column: str) -> str:
    """**这一列**历史上全 NULL 的那个标的（按列分配，6 位数字，表内造出来的）。

    为什么按列分配而不是共用一只票：判据要问"这一列登记成什么"，
    而**登记表的内容会变**（那正是它存在的意义）。按列给每个列一只专属标的，
    判据才能在运行期现挑一列（`legitimate` 的、`hole` 的、未登记的），
    不必把"哪一列登记成什么"抄进判据里（抄一份 ⇒ 表改了判据不跟着变 ⇒ 假绿）。
    """
    order = list(_QUANT_COLUMN_INDICATORS.values())
    assert column in order, f"{column} 不在事实源 {_QUANT_COLUMN_INDICATORS} 里"
    return f"9{order.index(column):05d}"


def _registered_columns(null_is: str) -> list[str]:
    """**现读登记表**：`_QUANT_COLUMN_INDICATORS` 覆盖到的列里，登记成 `null_is` 的那些。"""
    return [c for c in _QUANT_COLUMN_INDICATORS.values()
            if (null_policy.policy_for(c) or {}).get("null_is") == null_is]


def _unregistered_columns() -> list[str]:
    """**现读登记表**：`_QUANT_COLUMN_INDICATORS` 覆盖到的列里，没有登记的那些。"""
    return [c for c in _QUANT_COLUMN_INDICATORS.values()
            if null_policy.policy_for(c) is None]


# ============================================================
# 执行记录：证明"探针只在空结果时才跑"（判据 4、6）
# ============================================================


class _Ran:
    """一次运行里**真实发出去**的 SQL 与参数（按连接期累计）。"""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.connections = 0
        self.closed = 0

    def count(self, key: str) -> int:
        """按"这条 SQL 问的是哪一列"计数（见 `_sql_key`）。"""
        counts: dict[str, int] = {}
        for sql, _params in self.statements:
            k = _sql_key(sql)
            counts[k] = counts.get(k, 0) + 1
        return counts.get(key, 0)

    def probe_codes(self) -> list[Any]:
        """每条**探针**查询问的主体（顺序即执行顺序）。"""
        return [params[0] for sql, params in self.statements if _sql_key(sql) == "*"]


def _main_codes(ran: _Ran) -> list[Any]:
    """每条**主查询**问的主体 —— 与 `probe_codes()` 比对，防"两条 SQL 各拼一份主体"。"""
    return [params[0] for sql, params in ran.statements if _sql_key(sql) != "*"]


def _sql_key(sql: str) -> str:
    """这条 SQL 问的是哪一列？`*` = 聚合探针。

    为什么按**列**而不是按整条 SQL 文本比对：SQL 文本会随实现微调（加空格、
    换参数顺序），按文本比会把判据变成"你得逐字照抄我的实现"；而"这次跑了几条
    查询、分别问的是哪一列"是**语义**，正是我们要钉的东西。
    """
    if sql.lstrip().upper().startswith("SELECT COUNT("):
        return "*"
    head = sql[:sql.index(" FROM ")]
    return ", ".join(c.strip().strip('"') for c in head[len("SELECT "):].split(", "))


def _select_clause(column: str) -> str:
    """主查询的取值列 —— 从**事实源**（表结构）算出形状，不抄字面量。"""
    return _sql_key(f'SELECT trade_date, "{column}" FROM x')


def _expected_range_sql(column: str) -> str:
    """带区间的完整主查询**长什么样**（按事实源逐段拼，不抄实现的整条文本）。

    为什么要断整条：文件里那条纪律是"表名/列名/区间必须来自既有事实源"，
    而"来自事实源"只能靠**形状**钉 —— 谁把表名或参数抄成第二份，这里当场红。
    """
    return (f'SELECT trade_date, "{column}" FROM "{_QUANT_BASIC_TABLE}" '
            f'WHERE code = ? AND "{column}" IS NOT NULL '
            f"AND trade_date >= ? AND trade_date <= ? ORDER BY trade_date")


class _Recorder:
    """`sqlite3.Connection` 的**计数代理**：只转发 `execute` / `close`，并记下每条 SQL。

    为什么必须是代理而不是给连接打补丁：`sqlite3.Connection` 是**不可变 C 类型**
    ——按实例改报 `AttributeError: ... attribute 'execute' is read-only`，
    按类改报 `TypeError: cannot set 'execute' attribute of immutable type`
    （本轮两次实测都撞上了）。所以只能在 `connect` 这一层包一层，
    连接本身仍是真实实现（SQL 语法、类型、错误路径都没被替掉）。
    """

    def __init__(self, con: Any, ran: _Ran) -> None:
        self._con, self._ran = con, ran

    def execute(self, sql: str, params: Any = ()) -> Any:
        self._ran.statements.append((sql, tuple(params or ())))
        return self._con.execute(sql, params)

    def close(self) -> Any:
        self._ran.closed += 1
        return self._con.close()


def _record_sql(monkeypatch: pytest.MonkeyPatch) -> _Ran:
    """把 `sqlite3.connect` 包成计数代理，记下每条真实发出的 SQL。

    为什么不替身掉"数据库"：替身会把"SQL 拼错了"一起替掉 —— 而本判据要守的
    正是"表名/列名/主体条件来自事实源、并且真的这么查"。
    """
    ran = _Ran()
    real_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        ran.connections += 1
        return _Recorder(real_connect(*args, **kwargs), ran)

    monkeypatch.setattr(sqlite3, "connect", connect)
    return ran


# ============================================================
# 临时行情仓（进程内；`tmp_dir` 是既有 conftest 夹具）
# ============================================================


def _create_quant_table(con: Any, table: str, rows: list[tuple[Any, ...]]) -> None:
    """按**事实源**给出的列建表并灌行（表名参数化，供判据 6 造改名后的表）。"""
    cols = ", ".join(f'"{c}" TEXT' for c in _COLUMNS)
    con.execute(f'CREATE TABLE "{table}" ({cols})')
    con.executemany(
        f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(_COLUMNS))})', rows)
    con.commit()


@pytest.fixture
def warehouse(tmp_dir: str, monkeypatch: pytest.MonkeyPatch):
    """造一张 `quant_daily_basic`，并把行情仓指到它（走**真实**路径解析）。

    为什么走 `MOSS_QUANT_SQLITE` 而不是 patch `_quant_basic_db_path`：
    后者会把"库在哪"这条既有事实源一起替掉（`CHG-0059` 修的就是"读错了库"）。
    这里保留真实解析，只换库文件。
    """
    path = f"{tmp_dir}/warehouse.db"
    con = sqlite3.connect(path)
    try:
        rows: list[tuple[Any, ...]] = []
        values = {c: 1.0 for c in _QUANT_COLUMN_INDICATORS.values()}
        # ① 表内**有行**、`COLUMN` 历史上**有值**（都在判据用的区间之外）
        for i, day in enumerate(("20200102", "20200103", "20200106")):
            row = {c: values[c] + i for c in _QUANT_COLUMN_INDICATORS.values()}
            row["trade_date"], row["code"] = day, COVERED
            rows.append(tuple(row[c] for c in _COLUMNS))
        # ② 每一列各有一只"**表内有行、但这一列历史上全 NULL**"的专属标的
        #    （判据在运行期现挑一列：登记成 legitimate / hole / 未登记 三种都要有靶子）
        for column in _QUANT_COLUMN_INDICATORS.values():
            empty = {c: 2.0 for c in _QUANT_COLUMN_INDICATORS.values()}
            empty[column] = None
            empty["trade_date"] = "20200102"
            empty["code"] = _all_null_code(column)
            rows.append(tuple(empty[c] for c in _COLUMNS))
        _create_quant_table(con, _QUANT_BASIC_TABLE, rows)
    finally:
        con.close()

    monkeypatch.setenv("MOSS_QUANT_SQLITE", path)
    return path


def _assert_real_failure(exc: BaseException, *, why: str) -> None:
    """真失败的三条断言：不是结构化豁免、没有 `kind`、分类器**也不许**豁免它。

    最后一条最关键（也是判据的**主断言**）：豁免靠 `kind` **或**文本标记，
    只要文案里混进了标记，真缺口就会被说成"本来就没有" —— 多豁免比多报假阳性
    危险得多。所以这里必须问**真实分类器**，而不是只看异常类型。
    """
    assert not isinstance(exc, NoApplicableData), f"{why}：被标成了豁免类异常：{exc}"
    assert getattr(exc, "kind", None) is None, f"{why}：带上了豁免 kind：{exc}"
    assert classify_gap_kind(exc) is None, (
        f"{why}：被审计侧豁免了 —— 文本里混进了豁免标记（多豁免是危险方向）：{exc}")


# ============================================================
# ① 「表里没这只票」⇒ 结构化 not_covered
# ============================================================


@pytest.mark.parametrize("prefix", list(_QUANT_COLUMN_INDICATORS))
def test_code_absent_from_table_is_structured_not_covered(
    warehouse: str, monkeypatch: pytest.MonkeyPatch, prefix: str,
) -> None:
    """★ 探针① 无行 ⇒ `NoApplicableData(kind="not_covered")`（**不是**取数故障）。

    三条同时成立才算合格：
      ① 结构化 `kind` 与 `NoApplicableData.KINDS` 同源（不是自造取值）；
      ② 仍是 `DataFetchError` 子类 ⇒ 既有 `except DataFetchError` 拦截面不漏接；
      ③ 文案用的是**未收录**那一档的标记，且**不串**到"不适用"（两档对客户的
         说法不同：『口径不存在』vs『这张表不含它』）。

    逐列参数化：`_QUANT_COLUMN_INDICATORS` 的**每一个**列名都要真的能查到这张
    表（列名只能来自事实源 —— 判据自己抄一份列名就会与实现漂移）。
    """
    column = _QUANT_COLUMN_INDICATORS[prefix]
    ran = _record_sql(monkeypatch)

    with pytest.raises(NoApplicableData) as ei:
        AkshareConnector()._quant_column_points(
            f"{prefix}:{NOT_COVERED}", NOT_COVERED, column, None, None)
    exc = ei.value

    assert isinstance(exc, DataFetchError), "它仍必须是取数错误的一种（拦截面不许变）"
    assert exc.kind == "not_covered" and exc.kind in NoApplicableData.KINDS
    assert classify_gap_kind(exc) == GAP_KIND_NOT_COVERED, (
        f"真实分类器不认它是『未收录』：{exc}")
    assert NoApplicableData.MARKER_NOT_COVERED in str(exc)
    assert NoApplicableData.MARKER_NOT_APPLICABLE not in str(exc), (
        "不许串到「语义不适用」那一档 —— 对客户的说法不同")
    assert NOT_COVERED in str(exc), "文案里要能看出是**哪个主体**没被收录"
    assert "≠" in str(exc), "必须写明『表里没有』≠『取值为 0』（否则会被读反）"

    # 证据来自探针①：它问的必须是**同一个主体**（不许各拼一份）
    assert ran.count("*") == 1, f"探针①应该恰好跑一次：{ran.count('*')}"
    assert ran.count(_select_clause(column)) == 1, (
        f"主查询应该恰好跑一次：{ran.statements}")
    assert _main_codes(ran) == ran.probe_codes() == [NOT_COVERED], (
        f"探针① 与主查询问的主体不一致：主={_main_codes(ran)} 探针={ran.probe_codes()}")


# ============================================================
# ②③④ 第②支：分不分得开**只看登记表**（用户 2026-10-02 裁定 1）
# ============================================================


def test_unregistered_column_stays_a_defect_not_an_exemption(
    warehouse: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 防**多豁免**（这是裁定 1 的**默认方向**）：有行、该列历史全 NULL、
    但**没有**在 `configs/column_null_policy.yaml` 登记 ⇒ 仍是普通 `DataFetchError`。

    为什么默认必须是这一档：登记表是**豁免名单**。默认豁免 = 多豁免，
    而多豁免会把真缺口（该入库没入库）说成"本来就没有" —— 从此没人去修，
    缺陷清单里也看不到它（**没有任何症状**，比多报假阳性难发现得多）。

    终局判据是**真实分类器**不认它是豁免；异常类型只是附带。
    判据用的列**现读登记表**挑（不抄"哪一列未登记"）：把某一列登记进表里，
    这条判据会自动换一列继续守默认方向，而不会静默失效。
    """
    candidates = _unregistered_columns()
    assert candidates, (
        "`_QUANT_COLUMN_INDICATORS` 覆盖的列**全部**被登记了 —— 默认方向没有靶子，"
        "这条判据会变成永远绿（真出现了就说明该重新裁一次默认值）")
    column = candidates[0]
    code = _all_null_code(column)
    ran = _record_sql(monkeypatch)
    with pytest.raises(DataFetchError) as ei:
        AkshareConnector()._quant_column_points(
            f"{_PREFIX_OF[column]}:{code}", code, column, None, None)
    exc = ei.value

    _assert_real_failure(exc, why=f"未登记列 {column} 历史全 NULL")
    # 两条探针都要跑：① 证明"有行"，② 证明"这一列从来没值"
    assert ran.count("*") == 2, ran.statements
    assert ran.probe_codes() == [code, code], (
        f"两条探针问的必须是同一个主体：{ran.probe_codes()}")
    assert "登记" in str(exc), (
        "文案要把人指到登记表上（否则下一个人还会以为'分不开'是死结）")
    assert "取数失败" in str(exc), "处置要写清楚：先查入库链，不是改口径"


def test_registered_legitimate_column_is_structured_not_applicable(
    warehouse: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 反向的失效（裁定 1 要修的那件事）：登记为 `legitimate` 的列历史全 NULL
    ⇒ `NoApplicableData(kind="not_applicable")`（**不是**缺陷）。

    这一支的证据不在代码里、也不在猜语义里，而在**登记表**里：
    `configs/column_null_policy.yaml` 的条目带着 `why` + 可复现的 `evidence`
    （如 `dv_ttm`：股息率 = 分红 ÷ 股价，没分红就没有这个值 ——
    `src/infrastructure/connectors/akshare_connector.py:465-466` 在"没有已实施派息"
    时返回空而不是 0）。

    三条同时成立才算合格：
      ① 结构化 `kind` 与 `NoApplicableData.KINDS` 同源；
      ② 真实分类器认它是 `not_applicable`（不是靠文案）；
      ③ 文案用的是**不适用**那一档的标记，且**不串**到"未收录"（两档处置不同）。
    """
    registered = _registered_columns(null_policy.LEGITIMATE)
    assert registered, "没有任何 legitimate 登记 ⇒ 这条判据没有靶子（会永远绿）"
    column = registered[0]
    code = _all_null_code(column)
    ran = _record_sql(monkeypatch)

    with pytest.raises(NoApplicableData) as ei:
        AkshareConnector()._quant_column_points(
            f"{_PREFIX_OF[column]}:{code}", code, column, None, None)
    exc = ei.value

    assert isinstance(exc, DataFetchError), "它仍必须是取数错误的一种（拦截面不许变）"
    assert exc.kind == "not_applicable" and exc.kind in NoApplicableData.KINDS
    assert classify_gap_kind(exc) == GAP_KIND_NOT_APPLICABLE, (
        f"真实分类器不认它是『不适用』：{exc}")
    assert NoApplicableData.MARKER_NOT_APPLICABLE in str(exc)
    assert NoApplicableData.MARKER_NOT_COVERED not in str(exc), (
        "不许串到「未收录」那一档 —— 两档对客户的说法不同")
    assert "登记" in str(exc), "文案要写明依据是登记表（不是就地猜语义）"
    assert ran.count("*") == 2, "探针路径不变：①有行 + ②这一列历史没值"


def test_registered_hole_column_stays_a_defect(
    warehouse: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 登记为 `hole`（数据洞）的列历史全 NULL ⇒ 仍是普通 `DataFetchError`。

    `hole` 的意思就是"这个 NULL 是**该入库没入库**" —— 登记它**不是为了豁免**，
    而是把方向写下来（与未登记同向：fail-closed）。若这条判据红了，
    说明实现把 `hole` 也当成了豁免，等于**把该修的洞说成正常**。
    """
    registered = _registered_columns(null_policy.HOLE)
    assert registered, "没有任何 hole 登记 ⇒ 这条判据没有靶子（会永远绿）"
    column = registered[0]
    code = _all_null_code(column)

    with pytest.raises(DataFetchError) as ei:
        AkshareConnector()._quant_column_points(
            f"{_PREFIX_OF[column]}:{code}", code, column, None, None)
    _assert_real_failure(ei.value, why=f"hole 登记列 {column} 历史全 NULL")


def test_column_has_values_but_out_of_range_stays_a_defect(
    warehouse: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 防**多豁免**：有行、该列历史上有值、只是**请求区间内没有** ⇒ 普通 `DataFetchError`。

    这一支是真缺口（区间/新鲜度），豁免它等于把"放宽区间就能拿到"的数据
    说成"这个口径本来就没有" —— 与用户报障的那类误判互为镜像。
    ⚠️ 它与登记表**无关**（登记表只管"历史上全 NULL"那一支）：所以即使 `COLUMN`
    已被登记为合法无值，这一支也必须保持普通失败。
    """
    ran = _record_sql(monkeypatch)
    with pytest.raises(DataFetchError) as ei:
        AkshareConnector()._quant_column_points(
            INDICATOR, COVERED, COLUMN, "2026-01-01", "2026-12-31")
    exc = ei.value

    _assert_real_failure(exc, why="区间内没有")
    assert "区间" in str(exc)
    assert ran.count("*") == 2, "同样是两条探针（①有行 + ②历史有值）"


# ============================================================
# ③ 热路径不变（探针只在空结果时才跑）
# ============================================================


def test_probes_run_only_on_the_empty_path(
    warehouse: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 非空路径上**一次额外查询都没有**（防"为了观测把热路径变慢"）。

    两条断言缺一不可：
      ① 条数：只有主查询那一条（探针一次都没跑）；
      ② 形状：主查询的表名 / 列名 / 主体条件 / 区间**都来自事实源** ——
         "从既有事实源拼 SQL"这条纪律只能靠形状来钉（否则表名可以悄悄抄第二份）。
    """
    ran = _record_sql(monkeypatch)
    points = AkshareConnector()._quant_column_points(
        INDICATOR, COVERED, COLUMN, "2020-01-02", "2020-01-03")

    assert [p.value for p in points] == [1.0, 2.0], "正常路径的取值不许被顺手改了"
    assert [p.period_date for p in points] == ["2020-01-02", "2020-01-03"]
    assert all(isinstance(p, DataPoint) for p in points)

    assert ran.count("*") == 0, (
        f"非空路径上多跑了查询（探针跑进热路径了？）：{ran.statements}")
    assert ran.count(_select_clause(COLUMN)) == 1, (
        f"主查询应该恰好一次：{ran.statements}")
    sql, params = ran.statements[0]
    assert sql == _expected_range_sql(COLUMN), (
        "主查询的形状变了 —— 表名/列名/排序都必须来自既有事实源")
    assert params == (COVERED, "20200102", "20200103"), (
        f"主体与区间必须来自调用参数（不许在 SQL 里另写一份）：{params}")
    # 连接必须关上（探针多跑两次后更容易漏掉 `close`，而漏掉会攒住行锁）
    assert ran.connections == ran.closed == 1, (
        f"连接数 {ran.connections} / 关闭数 {ran.closed}")


# ============================================================
# ④ 既有拦截面不破 + 标记跨层可读
# ============================================================


class _RealProducerConnector:
    """把**真实抛点**接进路由器：异常由真实方法产出，不是手写样本。"""

    source_name, source_url = "akshare-quant", "test://akshare-quant"

    def __init__(self, prefix: str, column: str) -> None:
        self._prefix, self._column = prefix, column

    async def fetch(self, indicator: str, start_date: str | None = None,
                    end_date: str | None = None) -> list[DataPoint]:
        code = indicator.split(":", 1)[1]
        return AkshareConnector()._quant_column_points(
            indicator, code, self._column, start_date, end_date)

    def get_capabilities(self) -> dict[str, Any]:
        return {"name": self.source_name, "indicators": [self._prefix]}


class _SuccessConnector:
    """链上的下一个源：它必须**真的被走到**（证明故障转移没坏）。"""

    source_name, source_url = "stub-ok", "test://stub-ok"

    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, indicator: str, start_date: str | None = None,
                    end_date: str | None = None) -> list[DataPoint]:
        self.calls += 1
        return [DataPoint(indicator=indicator, value=1.0, period_date="2026-09-01",
                          source_name="stub")]

    def get_capabilities(self) -> dict[str, Any]:
        return {"name": self.source_name, "indicators": ["股息率TTM"]}


async def test_router_interception_unchanged_and_marker_survives(
    warehouse: str,
) -> None:
    """★ 两件事一起守（与 `test_gap_kind_producers` 的同名判据同一形状）：

    ① `NoApplicableData` 是 `DataFetchError` 子类 ⇒ 路由器**照样换源**
       （既有 `except DataFetchError` 拦截点行为不变，不许"漏接"）；
    ② 所有源都是"未收录"时，路由器把错误聚合成一条新 `DataFetchError`
       —— **类型会丢，标记不许丢**：`classify_gap_kind` 仍要认出 `not_covered`。
    """
    from src.infrastructure.connectors.router import ConnectorRouter

    ok = _SuccessConnector()
    router = ConnectorRouter(
        [(_RealProducerConnector("股息率TTM", COLUMN), lambda i: i.startswith("股息率TTM")),
         (ok, lambda i: i.startswith("股息率TTM"))],
        disable_cache=True, disable_db=True)

    assert await router.fetch(f"股息率TTM:{NOT_COVERED}"), "路由没能换到下一个源"
    assert ok.calls == 1, "结构化异常被漏接了 ⇒ 路由不再换源（拦截面破了）"

    all_fail = ConnectorRouter(
        [(_RealProducerConnector("股息率TTM", COLUMN), lambda i: True)],
        disable_cache=True, disable_db=True)
    with pytest.raises(DataFetchError) as ei:
        await all_fail.fetch(f"股息率TTM:{NOT_COVERED}")
    aggregated = ei.value
    assert not isinstance(aggregated, NoApplicableData), "聚合后类型本就该丢（既有行为）"
    assert NoApplicableData.MARKER_NOT_COVERED in str(aggregated)
    assert classify_gap_kind(aggregated) == GAP_KIND_NOT_COVERED, (
        "类型丢了就认不出来 ⇒ 『表里没这只票』又回到缺陷清单")


# ============================================================
# ⑤ 失败方向：探针自己读不出来 ⇒ 绝不许变成"未收录"
# ============================================================


def test_probe_failure_is_a_defect_not_a_coverage_gap(
    warehouse: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 探针**读失败**不许退化成"空" ⇒ 不许报 `not_covered`。

    把"读不出来"当成"不在表里"，就会把**一次数据源故障**说成
    「该专题未收录本主体（非缺陷）」—— 真故障被豁免掉、之后没人去修，
    正是本项目最贵的那类错误。这里让**探针**那一句炸掉、主查询照常返回空，
    看结论是否仍然 fail-closed。
    """
    real_connect = sqlite3.connect
    ran = _Ran()

    def boom(*_a: Any, **_k: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    class _ExplodingProbe(_Recorder):
        def execute(self, sql: str, params: Any = ()) -> Any:
            # **先记账再炸**：`count("*")` 要证明"探针确实被调用了"
            self._ran.statements.append((sql, tuple(params or ())))
            # 只炸**探针**（聚合查询）；主查询必须真的跑过（否则这条判据没靶子）
            if _sql_key(sql) == "*":
                boom()
            return self._con.execute(sql, params)

    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **k: _ExplodingProbe(real_connect(*a, **k), ran))
    with pytest.raises(DataFetchError) as ei:
        AkshareConnector()._quant_column_points(
            f"股息率TTM:{NOT_COVERED}", NOT_COVERED, COLUMN, None, None)
    _assert_real_failure(ei.value, why="探针读失败")
    assert "探针" in str(ei.value), "要把失败原因指到探针上（可排查）"
    assert ran.count("*") == 1, (
        f"探针根本没被调用 / 或不止一次 —— 这条判据没接在真实路径上：{ran.statements}")
    assert ran.count(_select_clause(COLUMN)) == 1, (
        f"主查询必须真的跑过（否则没有靶子）：{ran.statements}")


# ============================================================
# ⑥ 表名单一事实源（改一处必须红）
# ============================================================


def test_table_identity_is_a_single_fact_source(
    warehouse: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 表名**只有一份**：主查询与两条探针问的必须是**同一个常量**。

    做法可执行、可复现：在同一个库里**按改名后的表名**建一张空表，再把
    `_QUANT_BASIC_TABLE` 换掉 —— 于是主查询与探针都必须改口去查那张新表
    （查得到 ⇒ 走到探针① ⇒ 报 `not_covered`）。谁把表名抄成字面量，
    这条当场红：字面量会去查**旧**表（那里有行）⇒ 结论完全反过来。

    为什么建表而不是"让它报错"：改名后表不存在的话，主查询自己就会先抛
    「库不可读」，探针根本跑不到 —— 那条判据会**永远绿**（还是假绿）。
    """
    from src.infrastructure.connectors import akshare_connector as mod

    fake = f"{_QUANT_BASIC_TABLE}_renamed_by_test"
    con = sqlite3.connect(warehouse)
    try:
        _create_quant_table(con, fake, [])          # 空表 = 不收录任何主体
    finally:
        con.close()

    monkeypatch.setattr(mod, "_QUANT_BASIC_TABLE", fake)
    ran = _record_sql(monkeypatch)

    with pytest.raises(NoApplicableData) as ei:
        AkshareConnector()._quant_column_points(
            f"股息率TTM:{NOT_COVERED}", NOT_COVERED, COLUMN, None, None)
    assert ei.value.kind == "not_covered", (
        f"表名换了以后结论没跟着换 —— 三条 SQL 没共用同一个事实源：{ei.value}")

    assert ran.count("*") == 1, f"探针①应该跑过一次：{ran.statements}"
    assert ran.count(_select_clause(COLUMN)) == 1, (
        f"主查询应该跑过一次：{ran.statements}")
    for sql, _params in ran.statements:
        assert f'"{fake}"' in sql, f"这条 SQL 没跟着事实源改口（表名被抄了第二份）：{sql}"
        assert f'"{_QUANT_BASIC_TABLE}"' not in sql, (
            f"这条 SQL 还在查旧表名（说明有一处是字面量）：{sql}")


# ============================================================
# ⑨ ETF 那两档**不许互换**（用户 2026-10-02 裁定 2）
# ============================================================

#: 科创板 ETF（`_is_etf_code` 认得）—— 个股截面表里当然没有它。
ETF_CODE = "588170"


def test_etf_two_gates_are_not_swapped(
    warehouse: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ ETF 请求个股口径时按**语义分两档**（两个闸门、两个 `kind`，不许互换）：

    | 闸门 | 例子 | 语义 | `kind` |
    |---|---|---|---|
    | `_FIN_RATIO_INDICATORS` | `流动比率` | ETF 没有资产负债表 ⇒ **口径对它不存在** | `not_applicable` |
    | `_DERIVED_* + _QUANT_COLUMN_*` | `总市值`/`股息率TTM` | 口径**存在**，是我们的表/链**不收录 ETF** | `not_covered` |

    用户 2026-10-02 裁定 2 的原话是「算**覆盖问题**」，问的就是 `docs/PRD.md`
    §33.5 第 2 条点名的 `总市值:588170` 那一个判点（"会先命中 ETF 检查、
    永远走不到新探针"）—— 所以档二的 `kind` 是 `not_covered`。

    ## 为什么"互换"是最危险的失效

    两档对客户的说法**正好相反**，而处置也相反：
      · 把档二标成 `not_applicable` ⇒ 把"我们没收 ETF"说成"这个口径本来就没有"
        ＝**多豁免**（真缺口被抹掉，而且抹得很自然）；
      · 把档一标成 `not_covered` ⇒ 把"口径不存在"说成"我们这张表没收它"
        ⇒ 有人会去给一张**根本不该收 ETF** 的表加 ETF。
    所以这里对两档各下互斥断言（`MARKER_*` 不许串），而不是只看异常类型。

    另外两条一起守：
      · ETF 被拒时**一次都不许**查个股截面表（防误导性数据）；
      · 文案**逐字保留** `无个股{prefix}财务/基本面指标` —— 构造函数据它拼标记
        （`NoApplicableData.__init__` 把 `MARKER_*` 追加在文案后面）。
    """
    ran = _record_sql(monkeypatch)
    conn = AkshareConnector()

    # 档一：口径对 ETF **不存在** ⇒ not_applicable（本条判据**不动**它，只钉住不许被换走）
    with pytest.raises(NoApplicableData) as ei:
        conn._stock_fundamental(None, f"流动比率:{ETF_CODE}", None, None)
    first = ei.value
    assert first.kind == "not_applicable", f"档一被改判了：{first}"
    assert classify_gap_kind(first) == GAP_KIND_NOT_APPLICABLE
    assert NoApplicableData.MARKER_NOT_APPLICABLE in str(first)
    assert NoApplicableData.MARKER_NOT_COVERED not in str(first), (
        "口径『不存在』被说成了『我们没收录』—— 两档互换")
    assert "无个股" in str(first), "原文案必须逐字保留"

    # 档二：口径对 ETF **存在**、是我们的表/链不收录该主体 ⇒ not_covered（覆盖问题）
    for indicator in ("总市值", "股息率TTM", "换手率", "股息率"):
        with pytest.raises(NoApplicableData) as ei:
            conn._stock_fundamental(None, f"{indicator}:{ETF_CODE}", None, None)
        exc = ei.value
        assert isinstance(exc, DataFetchError), "必须仍是取数错误（拦截面不许变）"
        assert exc.kind == "not_covered", f"{indicator} 的档位变了：{exc}"
        assert exc.kind in NoApplicableData.KINDS
        assert classify_gap_kind(exc) == GAP_KIND_NOT_COVERED, (
            f"{indicator}：真实分类器不认它是『未收录』：{exc}")
        assert NoApplicableData.MARKER_NOT_COVERED in str(exc)
        assert NoApplicableData.MARKER_NOT_APPLICABLE not in str(exc), (
            f"{indicator}：覆盖问题 ≠ 口径不存在（两档不许互换）")
        assert f"ETF({ETF_CODE})无个股{indicator}财务/基本面指标" in str(exc), (
            "原文案必须逐字保留（构造函数据它拼标记）")

    assert not ran.statements, "ETF 被拒时一次都不许查个股截面表（防误导性数据）"
