"""采集异常**种类枚举 / 标签 / 静默改写**的离线护栏（`CHG-0120`）。

## 这份测试守的是什么

`collection_anomalies` 是管理员「运行指标 → 数据采集异常」区的数据源，
而它的 `record()` 里有一个**很深的兜底**：

```python
if kind not in KINDS:
    kind = KIND_GAP      # ← 未知 kind 静默变成"采集缺口"
```

这个兜底保住了"异常区不会因为一个错值就崩"，但它让
**拼错一个字母与真的采集缺口在界面上长得一模一样**
（本项目同类先例：`MOSS_SCHEDULER_DENY` 拼错作业名 ⇒ 两种完全不同的情况不可区分）。

| 判据 | 防的失效 |
|---|---|
| 未知 kind **必须告警**（且每个错值只告一次） | 静默改写 ⇒ 界面显示成"采集缺口"，而它其实是别的东西 |
| ★ **`kind_labels` 必须与 `KINDS` 一一对应**（派生，不写死） | 加了 kind 忘了加标签 ⇒ 界面显示原始英文串，用户以为坏了 |
| ★ **生产代码里出现的 kind 字面量必须都在 `KINDS` 里**（语法树现读） | 调用点写了 `"search_soruce"` 这类错值 ⇒ 静默变 gap |
| 搜索源故障 ⇒ `search_source`；有线索 ⇒ `source_lead` | 两者混进 `gap` ⇒ 排查方向跑偏（"某指标没数据" vs "找数据源的路断了"） |
| 观测失败**绝不抛** | 观测坏掉拖垮换源链 |

全部离线、临时目录，不碰真实 `data/run/`。
"""
from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest

from src.core import collection_anomalies as CA
from src.infrastructure.catalog import source_reroute as SR


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(CA, "_path", lambda root=None: tmp_path / CA.FILE_NAME)
    monkeypatch.setattr(CA, "_SEEN", {})
    monkeypatch.setattr(CA, "_WARNED_UNKNOWN_KINDS", set())
    return tmp_path


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


class _Outcome:
    def __init__(self, ok: bool, hits=None, blocked_by: str = "",
                 reason: str = "", served_by: str = "", spent: bool = False) -> None:
        self.ok = ok
        self.hits = hits or []
        self.blocked_by = blocked_by
        self.reason = reason
        self.served_by = served_by
        self.spent = spent


# ─────────────── ★ 静默改写：必须吭声 ───────────────
def test_unknown_kind_is_coerced_but_warns(env, caplog):
    """兜底保留（不崩），但**必须告警**且说清"会被显示成采集缺口"。"""
    with caplog.at_level(logging.WARNING):
        ok = CA.record("search_soruce", "CPI", "拼错的 kind")  # 故意拼错
    assert ok is True, "兜底应仍然记录（不许因为一个错值就崩）"
    recs = _records(env / CA.FILE_NAME)
    assert recs and recs[0]["kind"] == CA.KIND_GAP, "未知 kind 应被改写成 gap（既有行为）"
    msgs = " ".join(r.message for r in caplog.records)
    assert "search_soruce" in msgs, "告警里必须带**那个错值**，否则不知道去改哪里"
    assert "采集缺口" in msgs, "要说明它会被界面显示成什么"


def test_unknown_kind_warns_only_once_per_value(env, caplog):
    """同一错值**只吭一次** —— 否则一个循环能把日志刷爆。"""
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            CA.record("typo_kind", "A", f"reason-{_}")
        CA.record("another_typo", "B", "r")
    warns = [r for r in caplog.records if "未知 kind" in r.message]
    assert len(warns) == 2, f"应每个错值一条，实际 {len(warns)} 条"


# ─────────────── ★ 派生判据：标签覆盖 KINDS ───────────────
def test_labels_cover_kinds_exactly():
    """★ `kind_labels` 与 `KINDS` **一一对应**（派生自源码，不写死清单）。

    只断言"新 kind 在 KINDS 里"是不够的：界面用的是 `kind_labels[k] ?? k`，
    **漏了标签不会报错，只会显示一个英文串** —— 用户会以为功能坏了。
    """
    import src.api.routes.metrics as M

    src = Path("src/api/routes/metrics.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    labels: set[str] = set()
    for node in ast.walk(tree):
        #: 找 `out["kind_labels"] = { ... }` 那个字典字面量
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            keys = [k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if set(keys) >= {"gap", "timeout"}:
                labels = set(keys)
    assert labels, "没能从 metrics.py 里读出 kind_labels（结构变了，护栏需要同步）"
    assert labels == set(CA.KINDS), (
        f"kind_labels 与 KINDS 不一致：\n"
        f"  只在 KINDS 里（界面会显示原始串）：{sorted(set(CA.KINDS) - labels)}\n"
        f"  只在 labels 里（永远不会出现）：{sorted(labels - set(CA.KINDS))}")


def _unregistered_kind_literals(src: str, known: set[str]) -> list[str]:
    """在**源码字符串**里找"未登记的 kind 字面量"。抽成函数是为了能自证。

    ## 判据为什么必须收窄（本轮实测的假告警）

    第一版判据是"**任何 `kind=` 关键字实参**的字符串值都必须在 `KINDS` 里"，
    它立刻报了 `src/orchestration/supervisor.py` 里两处 `kind='empty'`。
    但那是 **`format_gap_log(..., kind="empty")`** —— 采集**日志**的缺口类型
    （`empty` / `error`，见 `AGENTS.md` 的 `[采集缺口]` 口径），
    与 `collection_anomalies.KINDS` **是两套完全不同的枚举**。
    ⇒ **判据太宽 = 自造假红**（本项目原话："自写正则报 6 个假告警，
    差点去修没坏的东西"）。判据要适应代码，不要让代码迁就判据。

    收窄后的判据：只看**`record` 类调用的第一个位置实参**是不是字符串字面量
    —— 那才是"传了个可能拼错的 kind 进异常库"的真实形态。
    """
    tree = ast.parse(src)
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (fn.attr if isinstance(fn, ast.Attribute)
                else fn.id if isinstance(fn, ast.Name) else "")
        if name not in ("record", "_record_anomaly"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str) \
                and node.args[0].value not in known:
            bad.append(f"line {node.args[0].lineno}: kind={node.args[0].value!r}")
    return bad


def test_guard_selfproof_detects_a_bad_literal_and_ignores_other_enums():
    """★ **自证**：先喂一个"已知答案"，确认判据既抓得住真的、也不误伤别的枚举。

    没有这一条，上面那条判据可能**恒绿**（比如函数名匹配写错就永远找不到调用点）。
    """
    known = {"gap", "timeout"}
    bad_src = 'CA.record("search_soruce", ind, r)\n'
    assert _unregistered_kind_literals(bad_src, known), "拼错的 kind 没被抓到（判据恒绿）"

    ok_src = 'CA.record(CA.KIND_GAP, ind, r)\nCA.record("gap", ind, r)\n'
    assert _unregistered_kind_literals(ok_src, known) == [], "合法写法被误报"

    #: ★ 关键的反向对照：**别的枚举**（采集日志的 kind）不许被误伤
    other_enum = 'format_gap_log(ind, "…", kind="empty")\n'
    assert _unregistered_kind_literals(other_enum, known) == [], (
        "把 `format_gap_log(kind='empty')` 当成了采集异常 kind —— 自造假红")


def test_production_kind_literals_are_registered():
    """★ 生产代码里传给异常库的 kind 字面量**必须都在 `KINDS` 里**。

    上游风险：标签表对了，但调用点写了个不在 `KINDS` 的 kind，
    一样会被静默改写成 `gap`（所以这条与标签覆盖是**两件事**）。
    """
    files = [
        Path("src/infrastructure/catalog/source_reroute.py"),
        Path("src/orchestration/supervisor.py"),
        Path("src/scheduler/jobs.py"),
        Path("src/scheduler/catalog_jobs.py"),
    ]
    bad: list[str] = []
    for f in files:
        if not f.exists():
            continue
        bad += [f"{f} {b}" for b in
                _unregistered_kind_literals(f.read_text(encoding="utf-8"),
                                            set(CA.KINDS))]
    assert not bad, (
        "生产代码里出现了未登记的 kind 字面量（会被静默改写成 gap）：\n  "
        + "\n  ".join(bad))


# ─────────────── 两个新 kind 的语义 ───────────────
def test_search_failure_is_recorded_as_search_source_not_gap(env):
    """搜索源自己被拒 ⇒ `search_source`。**混进 gap 会让排查方向跑偏。**"""
    SR._record_anomaly("社融", _Outcome(False, blocked_by="budget",
                                       reason="额度闸门已关"), [])
    recs = _records(env / CA.FILE_NAME)
    assert len(recs) == 1
    assert recs[0]["kind"] == CA.KIND_SEARCH_SOURCE
    assert recs[0]["kind"] != CA.KIND_GAP, "搜索源故障被记成了采集缺口"
    assert recs[0]["extra"]["blocked_by"] == "budget"


def test_leads_are_recorded_as_source_lead_not_gap(env):
    """搜到候选网址 ⇒ `source_lead`（**不是故障**）。"""
    hits = [{"url": "https://a.example/1", "title": "某源", "snippet": "", "site": ""}]
    SR._record_anomaly("挖掘机销量", _Outcome(True, hits=hits, served_by="bocha"), hits)
    recs = _records(env / CA.FILE_NAME)
    assert recs and recs[0]["kind"] == CA.KIND_SOURCE_LEAD
    assert recs[0]["extra"]["count"] == 1
    assert recs[0]["extra"]["top_url"] == "https://a.example/1"


def test_successful_search_without_hits_records_nothing(env):
    SR._record_anomaly("X", _Outcome(True, hits=[]), [])
    assert _records(env / CA.FILE_NAME) == []


def test_recording_never_raises(env, monkeypatch):
    """观测坏掉**绝不许**拖垮换源链。"""
    def boom(*_a, **_k):
        raise RuntimeError("观测炸了")

    monkeypatch.setattr(CA, "record", boom)
    SR._record_anomaly("X", _Outcome(False, blocked_by="http", reason="r"), [])  # 不抛


# ─────────────── 端到端：discover 会把两类都落到文件里 ───────────────
@pytest.mark.asyncio
async def test_discover_records_search_source_when_blocked(env, tmp_path, monkeypatch):
    monkeypatch.setattr(SR, "_log_path", lambda root=None: tmp_path / "audit.jsonl")

    def blocked(q, count=5):  # noqa: ANN001
        return _Outcome(False, blocked_by="no_key", reason="未配置 bocha_search")

    hits, note = await SR.discover_candidate_urls("社保", search=blocked,
                                                  root=tmp_path)
    assert hits == []
    recs = _records(env / CA.FILE_NAME)
    assert recs and recs[0]["kind"] == CA.KIND_SEARCH_SOURCE
    assert "no_key" in note or "候选" in note


@pytest.mark.asyncio
async def test_discover_records_lead_when_found(env, tmp_path, monkeypatch):
    monkeypatch.setattr(SR, "_log_path", lambda root=None: tmp_path / "audit.jsonl")

    def good(q, count=5):  # noqa: ANN001
        return _Outcome(True, hits=[{"url": "https://ok.example/d", "title": "T",
                                     "snippet": "", "site": ""}], served_by="baidu")

    hits, _ = await SR.discover_candidate_urls("社保", search=good, root=tmp_path)
    assert len(hits) == 1
    recs = _records(env / CA.FILE_NAME)
    assert recs and recs[0]["kind"] == CA.KIND_SOURCE_LEAD


# ─────────────── ★ 测试不许污染生产面板 ───────────────
def test_anomaly_store_is_isolated_from_production_by_default():
    """★ `tests/conftest.py` 的 autouse 夹具必须把异常库指到**临时目录**。

    ## 这一条防的是本轮实测的事故

    管理员界面「数据采集异常」区读的是**生产**的
    `<run_dir>/collection_anomalies.jsonl`（`run_dir` 登记为 shared），
    而集成测试里那句 `raise RuntimeError("网络炸了")` 会被 Supervisor
    写进同一个文件 —— 实测**34 行里 32 行**是它产生的，
    即**面板 94% 是假数据**。

    判据写成"**不在生产 run_dir 下**"，而不是"等于某个具体临时路径"
    （后者会被 `mkdtemp` 的随机名弄成假红）。
    """
    from src.infrastructure.catalog.data_stores import PROJECT_ROOT

    p = Path(CA._path()).resolve()
    prod = (Path(PROJECT_ROOT) / "data" / "run").resolve()
    assert not p.is_relative_to(prod), (
        f"采集异常库指向了生产目录 {p} ⇒ 跑测试会把假错误写进管理员面板。"
        "检查 tests/conftest.py 的 `_isolate_collection_anomalies` 是否还在。")


def test_recording_in_a_test_does_not_touch_production_file(tmp_path):
    """落一笔，确认它落在**隔离目录**里、且生产文件行数不变。"""
    from src.infrastructure.catalog.data_stores import PROJECT_ROOT

    prod = Path(PROJECT_ROOT) / "data" / "run" / CA.FILE_NAME
    before = prod.read_text(encoding="utf-8").count("\n") if prod.exists() else 0

    CA.record(CA.KIND_SOURCE_LEAD, "隔离自证指标", "这条只应落在临时目录")

    after = prod.read_text(encoding="utf-8").count("\n") if prod.exists() else 0
    assert after == before, "测试把记录写进了生产异常文件 —— 面板会被污染"
    assert Path(CA._path()).exists(), "隔离目录里也没落盘 ⇒ 夹具把它弄丢了"
