"""博查搜索客户端的**离线**护栏（不联网、不花钱、确定性）。

## 这份测试守的是什么

用户提供的免费资源包是 **1000 次总量** —— 所以这个模块最贵的缺陷不是
"搜不准"，而是"**在某条循环里把额度悄悄烧光**"。因此判据的重心在**闸门**：

| 判据 | 防的失效 |
|---|---|
| 没配 key ⇒ **一次都不发** | 无凭据还硬打，每次都失败还刷日志 |
| 今日上限到了 ⇒ **一次都不发** | 某个循环一天烧光 |
| 总量上限到了 ⇒ **一次都不发** | 总量烧光后仍持续失败 |
| **预算文件损坏 ⇒ 按已用尽** | 闸门状态读不到时"默认放行"＝闸门失效 |
| 同一 query 第二次 ⇒ **命中缓存、不再花钱** | 重复问同一件事重复付费 |
| `allow_spend=False` ⇒ 只读缓存 | 巡检/预热路径误花钱 |
| 失败一律 `ok=False` + 人话原因、**不抛异常** | 搜索挂掉拖垮换源链路 |
| `count` 被夹在 1..20 | 传个大数把单次成本/响应体放大 |

★ 本文件的判据**全部用注入的假 transport**：真发一次请求就花一次钱，
"用真钱跑单测"在这个项目里是不可接受的（`AGENTS.md`：不花钱的测试用例）。
真实通路另有体检命令 `scripts/check_external_sources.py`（**显式**、带退出码）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from src.infrastructure.search import bocha

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@pytest.fixture()
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """只隔离**路径**（缓存目录 + 预算文件），**不动** `api_key`。

    ★ 为什么要把"路径隔离"与"凭据替身"拆成两个夹具：
    合成一个时，`api_key` 被替换掉，于是"凭据读取失败"这类判据根本走不到
    真实代码路径 —— **测试会假装通过**（本轮实测：判据红出来才发现）。
    夹具也要遵守"判据必须打在它真正读的那一层上"。
    """
    cache = tmp_path / "cache"
    run = tmp_path / "run"
    cache.mkdir()
    run.mkdir()

    def fake_cache_dir() -> Path:
        p = cache / "search"
        p.mkdir(exist_ok=True)
        return p

    monkeypatch.setattr(bocha, "_cache_dir", fake_cache_dir)
    monkeypatch.setattr(bocha, "_budget_path", lambda: run / "search_budget.json")
    #: 预算账本也给足余额，免得闸门先把它挡掉（那是另一条判据的事）
    monkeypatch.setattr(bocha, "MAX_CALLS_PER_DAY", 1000)
    monkeypatch.setattr(bocha, "MAX_CALLS_TOTAL", 1000)
    return tmp_path


@pytest.fixture()
def isolated(isolated_paths: Path, monkeypatch: pytest.MonkeyPatch):
    """路径隔离 + 一个**假凭据**（绝大多数用例只关心闸门与解析）。"""
    monkeypatch.setattr(bocha, "api_key", lambda: "sk-test-key")
    return isolated_paths


class _Recorder:
    """假 transport：记录被调用了几次，并返回预置响应。"""

    def __init__(self, status: int = 200, body: str = "") -> None:
        self.calls: list[dict] = []
        self.status = status
        self.body = body or json.dumps({"data": {"webPages": {"value": [
            {"name": "国家统计局", "url": "https://data.stats.gov.cn/x",
             "snippet": "月度数据", "siteName": "stats.gov.cn"},
            {"name": "不合法条目没有 url", "snippet": "应被过滤"},
        ]}}})

    def __call__(self, url, payload, headers, timeout):  # noqa: ANN001
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        return self.status, self.body


# ─────────────────────────── 闸门 ───────────────────────────
def test_missing_key_never_sends_a_request(isolated, monkeypatch):
    monkeypatch.setattr(bocha, "api_key", lambda: "")
    rec = _Recorder()
    out = bocha.web_search("x", transport=rec)
    assert out.ok is False and out.spent is False
    assert out.blocked_by == "no_key"
    assert rec.calls == [], "没配 key 却发了请求 —— 闸门失效"


def test_daily_cap_blocks_before_sending(isolated, monkeypatch):
    monkeypatch.setattr(bocha, "MAX_CALLS_PER_DAY", 2)
    bocha._budget_path().write_text(
        json.dumps({"date": bocha._today(), "calls_today": 2, "calls_total": 2}),
        encoding="utf-8")
    rec = _Recorder()
    out = bocha.web_search("x", transport=rec, use_cache=False)
    assert out.ok is False and out.blocked_by == "budget"
    assert rec.calls == []


def test_total_cap_blocks_before_sending(isolated, monkeypatch):
    monkeypatch.setattr(bocha, "MAX_CALLS_TOTAL", 5)
    bocha._budget_path().write_text(
        json.dumps({"date": bocha._today(), "calls_today": 0, "calls_total": 5}),
        encoding="utf-8")
    rec = _Recorder()
    out = bocha.web_search("x", transport=rec, use_cache=False)
    assert out.ok is False and out.blocked_by == "budget" and rec.calls == []


def test_corrupt_budget_file_fails_closed(isolated):
    """★ 预算文件坏掉时**按已用尽**处理 —— 绝不能"读不到就放行"。"""
    bocha._budget_path().write_text("{ 这不是 json", encoding="utf-8")
    rec = _Recorder()
    out = bocha.web_search("x", transport=rec, use_cache=False)
    assert out.ok is False, "预算文件损坏却放行了 —— 闸门失效"
    assert out.blocked_by == "budget"
    assert rec.calls == []


def test_allow_spend_false_reads_cache_only(isolated):
    rec = _Recorder()
    out = bocha.web_search("x", transport=rec, allow_spend=False)
    assert out.ok is False and out.blocked_by == "cache_only"
    assert rec.calls == []


# ─────────────────────────── 计费 ───────────────────────────
def test_success_parses_hits_and_counts_once(isolated):
    rec = _Recorder()
    out = bocha.web_search("指标 数据源", transport=rec)
    assert out.ok is True and out.spent is True and out.from_cache is False
    assert [h.url for h in out.hits] == ["https://data.stats.gov.cn/x"], \
        "没有 url 的条目必须被过滤掉"
    assert len(rec.calls) == 1
    st = bocha.budget_state()
    assert st["used_total"] == 1 and st["used_today"] == 1


def test_second_identical_query_hits_cache_and_does_not_spend(isolated):
    """★ 反复问同一件事**不许重复花钱**。"""
    rec = _Recorder()
    first = bocha.web_search("同一个问题", transport=rec)
    second = bocha.web_search("同一个问题", transport=rec)
    assert first.spent is True and second.from_cache is True
    assert second.ok is True and len(rec.calls) == 1, \
        "第二次又发了请求 —— 缓存没起作用（在花钱）"
    assert bocha.budget_state()["used_total"] == 1


def test_count_is_clamped(isolated):
    rec = _Recorder()
    bocha.web_search("x", count=9999, transport=rec)
    assert rec.calls[0]["payload"]["count"] == 20


# ─────────────────────────── 失败语义 ───────────────────────────
def test_http_error_returns_reason_and_never_raises(isolated):
    rec = _Recorder(403, json.dumps({"message": "no quota"}))
    out = bocha.web_search("x", transport=rec, use_cache=False)
    assert out.ok is False and "403" in out.reason and "no quota" in out.reason


def test_transport_exception_never_raises(isolated):
    def boom(*_a, **_k):
        raise TimeoutError("模拟挂住")

    out = bocha.web_search("x", transport=boom, use_cache=False)
    assert out.ok is False and "TimeoutError" in out.reason


def test_bad_body_never_raises(isolated):
    rec = _Recorder(200, "不是 json")
    out = bocha.web_search("x", transport=rec, use_cache=False)
    assert out.ok is False and "解析失败" in out.reason


def test_empty_query_does_not_spend(isolated):
    rec = _Recorder()
    out = bocha.web_search("   ", transport=rec)
    assert out.ok is False and rec.calls == []


# ─────────────── ★ 凭据来源：读得到 / 读不到 / 读失败 是三件事 ───────────────
def test_api_key_reads_from_settings(monkeypatch):
    """**回归**：`api_key()` 必须走真实的 `get_settings()`。

    原始缺陷：函数里写的是 `from src.core.config import settings`
    —— 那个名字**不存在**（真名是 `get_settings()`），而外面套着
    `except Exception: return ""`，于是 **ImportError 被吞成"未配置凭据"**。
    症状是"`.env` 里明明写着却报没配"，把排查方向整个带偏（实测花了两轮）。

    本判据用替身 Settings 断言取值路径：若实现又去导入一个不存在的名字，
    这里会直接失败，而不是伪装成"未配置"。
    """
    import src.core.config as cfg

    class _Stub:
        bocha_search_api_key = "sk-from-settings"

    monkeypatch.setattr(cfg, "get_settings", lambda: _Stub())
    assert bocha.api_key() == "sk-from-settings"


def test_config_failure_is_NOT_reported_as_unconfigured(isolated_paths, monkeypatch):
    """★★ 「配置读取失败」绝不许说成「未配置凭据」（`AGENTS.md`：没量到 ≠ 量到 0）。

    这是上面那个缺陷的**语义判据**：两者处置完全不同 ——
    一个去查凭据，另一个去修代码。合并成一个结论 = 把人引向错误方向。
    """
    import src.core.config as cfg

    def boom():
        raise RuntimeError("配置模块炸了")

    monkeypatch.setattr(cfg, "get_settings", boom)
    rec = _Recorder()
    out = bocha.web_search("x", transport=rec)
    assert out.ok is False
    #: ★ 判据只认**机器可读的判别位**，不认人话 ——
    #: `reason` 为了讲清楚会包含别的词，拿它做 `not in` 会自己造假红（本轮踩过）。
    assert out.blocked_by == "config_error", (
        f"配置故障必须报 config_error，实际 blocked_by={out.blocked_by!r}；"
        f"若报成 no_key 就是把'代码坏了'说成'凭据没配'：{out.reason}")
    assert rec.calls == []
