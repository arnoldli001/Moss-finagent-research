"""换源线索「上报」的离线护栏（`CHG-0118`）—— 不联网、不花钱、确定性。

## 这份测试守什么

这是给 path C 的产物补**消费方**。判据重心是**噪音纪律**与**结构完整性**：

| 判据 | 防的失效 |
|---|---|
| 同一条线索**只报一次** | 每天把同一批网址刷进日志（"用户抱怨邮件太多"的日志版） |
| 单轮**限速** `max_per_run` | 一次冒出一百条把日志淹掉 |
| 状态**落盘**且**有界** | 冷却放内存里重启即失效；状态无限长 |
| 审计行坏了**跳过不崩** | 一个脏字节让整个作业失败、运行台账刷红 |
| `read_leads` **绝不抛** | 换源是补救路径，不许被证据文件拖垮 |
| ★ **三件套齐**（JobKind + JobSpec + 分发分支 + handler） | 本项目真实踩过：作业在 `JOB_REGISTRY` 声明了，**执行器分发链没有分支** ⇒ 每次记"未知作业类型"失败 |
| ★ **线索≠可用源** | 上报的必须只是"待复核网址"，不许被当成已经能取数 |

全部用临时目录，**不碰真实 `data/run/`**。
"""
from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

from src.infrastructure.catalog import source_leads as SL
from src.scheduler import jobs as J


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把审计与状态文件都指到临时目录。"""
    audit = tmp_path / "source_reroute_log.jsonl"
    state = tmp_path / SL.STATE_NAME
    monkeypatch.setattr(SL, "_log_path", lambda root=None: audit)
    monkeypatch.setattr(SL, "_state_path", lambda root=None: state)
    return tmp_path


def _write_leads(path: Path, records: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _lead_record(indicator: str, urls: list[str], *, ts: float | None = None) -> dict:
    return {"ts": ts if ts is not None else time.time(),
            "kind": "search_candidates", "indicator": indicator,
            "note": "测试", "actionable": False, "next_step": "网址 → 连接器",
            "hits": [{"title": f"源_{u[-6:]}", "url": u, "snippet": "", "site": "e.com"}
                     for u in urls]}


class _Spec:
    def __init__(self, **params: object) -> None:
        self.params = params


# ─────────────────── 读取 ───────────────────
def test_missing_audit_returns_empty(env):
    assert SL.read_leads() == []


def test_reads_and_expands_each_url(env):
    _write_leads(env / "source_reroute_log.jsonl", [
        _lead_record("社融", ["https://a.example/1", "https://a.example/2"])])
    leads = SL.read_leads()
    assert [x["url"] for x in leads] == ["https://a.example/1", "https://a.example/2"]
    assert all(x["indicator"] == "社融" for x in leads)
    assert len({x["id"] for x in leads}) == 2, "不同网址必须有不同的稳定 id"


def test_corrupt_line_is_skipped_not_fatal(env):
    p = env / "source_reroute_log.jsonl"
    p.write_text("{ 这不是 json\n"
                 + json.dumps(_lead_record("CPI", ["https://b.example/x"])) + "\n",
                 encoding="utf-8")
    leads = SL.read_leads()
    assert len(leads) == 1, "一行坏数据不该让整次读取失败"


def test_other_kinds_are_ignored(env):
    """审计里还有**口径判定**记录（`kind` 不同）—— 不许混进线索。"""
    _write_leads(env / "source_reroute_log.jsonl", [
        {"ts": time.time(), "kind": "decision", "indicator": "CPI",
         "verdict": {"ok": True}, "hits": [{"url": "https://should-not.example"}]}])
    assert SL.read_leads() == []


def test_read_never_raises_even_if_file_unreadable(env, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("磁盘炸了")

    monkeypatch.setattr(SL.Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "read_text", boom)
    assert SL.read_leads() == [], "读失败必须返回空，不许抛给调度器"


# ─────────────────── 噪音纪律 ───────────────────
def test_surfaced_leads_are_not_reported_twice(env):
    _write_leads(env / "source_reroute_log.jsonl",
                 [_lead_record("社融", ["https://a.example/1"])])
    leads = SL.read_leads()
    assert len(SL.filter_new(leads)) == 1
    SL.mark_surfaced([leads[0]["id"]])
    assert SL.filter_new(SL.read_leads()) == [], "同一线索被重复上报了"


def test_state_is_persisted_and_bounded(env, monkeypatch):
    monkeypatch.setattr(SL, "MAX_STATE_ENTRIES", 3)
    SL.mark_surfaced([f"id{i}" for i in range(10)])
    raw = json.loads((env / SL.STATE_NAME).read_text(encoding="utf-8"))
    assert len(raw["surfaced"]) == 3, "状态文件必须有界"


def test_corrupt_state_falls_back_to_empty(env):
    """状态读不到 ⇒ 按空处理（最坏重复报一次），**不能**当成"都报过"永久静默。"""
    (env / SL.STATE_NAME).write_text("{坏", encoding="utf-8")
    _write_leads(env / "source_reroute_log.jsonl",
                 [_lead_record("社融", ["https://a.example/1"])])
    assert len(SL.filter_new(SL.read_leads())) == 1


def test_summarize_merges_by_indicator():
    leads = [{"indicator": "A"}, {"indicator": "A"}, {"indicator": "B"}]
    assert SL.summarize(leads) == [("A", 2), ("B", 1)]


# ─────────────────── 作业 handler ───────────────────
@pytest.mark.asyncio
async def test_job_reports_once_then_stays_quiet(env):
    _write_leads(env / "source_reroute_log.jsonl",
                 [_lead_record("社融", ["https://a.example/1"])])
    first, errs = await J._source_leads_audit(_Spec(max_per_run=10))
    assert first == 1 and errs == []
    second, errs2 = await J._source_leads_audit(_Spec(max_per_run=10))
    assert second == 0 and errs2 == [], "第二次又报了 —— 去重失效（会天天刷日志）"


@pytest.mark.asyncio
async def test_job_respects_rate_cap(env):
    _write_leads(env / "source_reroute_log.jsonl",
                 [_lead_record("社融", [f"https://a.example/{i}" for i in range(25)])])
    n, _ = await J._source_leads_audit(_Spec(max_per_run=10))
    assert n == 10, "单轮限速没生效 —— 一次冒出 25 条会淹掉日志"
    n2, _ = await J._source_leads_audit(_Spec(max_per_run=10))
    assert n2 == 10, "剩下的一轮轮报，不是丢掉"


@pytest.mark.asyncio
async def test_job_no_leads_is_not_an_error(env):
    n, errs = await J._source_leads_audit(_Spec())
    assert n == 0 and errs == []


@pytest.mark.asyncio
async def test_job_never_raises_when_reader_explodes(env, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("审计炸了")

    monkeypatch.setattr(SL, "read_leads", boom)
    n, errs = await J._source_leads_audit(_Spec())
    assert n == 0 and errs, "读失败要如实进 errors，但不许抛异常"


# ─────────────────── ★ 结构判据（三件套） ───────────────────
def test_job_is_wired_end_to_end():
    """★ 声明了 JobKind/JobSpec 却**忘了分发分支** ⇒ 每次都记"未知作业类型"失败。

    这是本项目真实踩过的坑（`AGENTS.md` 明写），所以用**语法树**断言三件套齐：
      ① `JobKind` 字面量里有它；② `JOB_REGISTRY` 有登记；
      ③ `jobs.py` 里既有 `if spec.kind == "..."` 分支、也有对应的 handler 函数。
    """
    from src.scheduler.registry import JOB_REGISTRY

    kind = "source_leads_audit"
    src_reg = Path("src/scheduler/registry.py").read_text(encoding="utf-8")
    assert f'"{kind}"' in src_reg, "JobKind 字面量里没有它"
    assert kind in JOB_REGISTRY, "JOB_REGISTRY 里没有登记"
    assert JOB_REGISTRY[kind].updates == (), (
        "只读作业不该声明存储写入 —— 声明了会被写权限裁剪误伤")

    src_jobs = Path("src/scheduler/jobs.py").read_text(encoding="utf-8")
    tree = ast.parse(src_jobs)
    #: 分发分支：`if spec.kind == "source_leads_audit":`
    branch = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute) \
                and node.left.attr == "kind":
            for c in node.comparators:
                if isinstance(c, ast.Constant) and c.value == kind:
                    branch = True
    assert branch, "jobs.py 里没有 `spec.kind == 'source_leads_audit'` 的分发分支"
    assert f"async def _source_leads_audit" in src_jobs, "handler 函数不存在"
    assert "_source_leads_audit(spec)" in src_jobs, "分支存在但没调用 handler"


def test_leads_are_marked_not_actionable():
    """线索 ≠ 可用源：审计里必须显式 `actionable=False`，否则会被当成已能取数。"""
    from src.infrastructure.catalog import source_reroute as sr

    src = Path("src/infrastructure/catalog/source_reroute.py").read_text(encoding="utf-8")
    assert '"actionable": False' in src, (
        "线索记录缺少 actionable=False —— 下一个人会把一个网址当成能用的源")
    assert hasattr(sr, "discover_candidate_urls")
