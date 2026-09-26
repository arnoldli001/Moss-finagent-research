"""LLM 审计的**租户归属**与 token 累计（`src/infrastructure/llm/audit.py`）。

## 为什么必须有这一层测试

套餐里有一项上限叫 `llm_tokens_per_month`（"这个租户一个月最多烧多少 token"）。
要显示"还剩多少"，就必须能回答"这个租户这个月用掉多少" —— 而这个数只能
从 LLM 审计累计，因为它是唯一逐次记录 `tokens_in`/`tokens_out` 的地方。

而这份审计**原来没有租户字段**（`src/infrastructure/security/rls.py` 的注释
却写着"已按租户写入 user_id/tenant_id 字段"）。于是：

  · 界面上"本月 token 用量"永远显示 0 —— 看起来像"这个客户一点没用"，
    而真相是"我们没记"；
  · 管理员据此判断"该不该给他加额度"，会做出完全错误的决定。

这类"假数据"比报错更难发现，所以这里逐条钉死：写的时候带归属、
读的时候按租户聚合、且**取不到身份时不能编一个用户**。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from src.core.tenancy import (
    DataClass,
    Principal,
    Role,
    WallGroup,
    principal_scope,
    system_scope,
)
from src.infrastructure.llm.audit import (
    LLMAuditLog,
    principal_ids,
)
from src.infrastructure.llm.models import LLMResponse


def _response(tokens_in: int = 10, tokens_out: int = 5) -> LLMResponse:
    return LLMResponse(
        content="ok", model_used="m", provider="p",
        prompt_hash="ph", response_hash="rh",
        tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=12,
    )


def _record(log: LLMAuditLog, **kw) -> dict:
    return log.record(trace_id="tr", agent_id="A08", task_tier="light",
                      response=_response(**kw), cached=False)


def _principal(tenant: str, user: str) -> Principal:
    return Principal(user_id=user, tenant_id=tenant,
                     roles=frozenset({Role.RESEARCHER}),
                     clearance=DataClass.INTERNAL,
                     wall_group=WallGroup.RESEARCH, auth_source="session")


# ======================================================================
# 一、写入时带归属
# ======================================================================

def test_record_writes_tenant_and_user_from_scope(tmp_path) -> None:
    """★ 核心：在租户身份下调用 → 审计里带上 tenant_id / user_id。"""
    log = LLMAuditLog(str(tmp_path))
    with principal_scope(_principal("tenant_acme", "u_acme_1")):
        entry = _record(log)
    assert entry["tenant_id"] == "tenant_acme"
    assert entry["user_id"] == "u_acme_1"

    on_disk = json.loads(log.path.read_text(encoding="utf-8").strip())
    assert on_disk["tenant_id"] == "tenant_acme", "只有内存里带了归属，没落盘"


def test_record_outside_scope_has_empty_attribution(tmp_path) -> None:
    """★ 取不到身份时写**空串**，绝不能写那个哨兵值。

    未认证时 `current_principal()` 返回哨兵对象，它的 `user_id` 字面量是
    `"__unauthenticated__"`。直接读字段就会把这个哨兵当成一个真实用户
    写进审计，统计里于是出现一个叫 `__unauthenticated__` 的"人"。
    """
    log = LLMAuditLog(str(tmp_path))
    entry = _record(log)
    assert entry["tenant_id"] == ""
    assert entry["user_id"] == ""
    assert "__unauthenticated__" not in json.dumps(entry)


def test_system_scope_is_attributed_to_system_user(tmp_path) -> None:
    """后台任务/调度跑的调用归到 `system:*`，不能混进客户租户的账单。"""
    log = LLMAuditLog(str(tmp_path))
    with system_scope("scheduler"):
        entry = _record(log)
    assert entry["user_id"] == "system:scheduler"


def test_principal_ids_helper_never_raises() -> None:
    """审计写入绝不能因为取身份失败而丢记录（钱已经花了）。"""
    assert principal_ids() == ("", "")


def test_cache_hit_calls_are_still_attributed(tmp_path) -> None:
    """命中缓存的调用没有真实 token 消耗，但**也要记归属** ——
    否则"这个月调了多少次"会漏，而配额判断不能只看 token。"""
    log = LLMAuditLog(str(tmp_path))
    with principal_scope(_principal("t", "u")):
        log.record(trace_id="tr", agent_id="A", task_tier="light",
                   response=_response(0, 0), cached=True)
    row = json.loads(log.path.read_text(encoding="utf-8").strip())
    assert (row["tenant_id"], row["cache_hit"]) == ("t", True)


# ======================================================================
# 二、按租户累计 token
# ======================================================================

def test_tokens_by_tenant_sums_in_and_out(tmp_path) -> None:
    log = LLMAuditLog(str(tmp_path))
    with principal_scope(_principal("t1", "u1")):
        _record(log, tokens_in=100, tokens_out=50)
        _record(log, tokens_in=10, tokens_out=5)
    with principal_scope(_principal("t2", "u2")):
        _record(log, tokens_in=7, tokens_out=3)

    usage, truncated = log.tokens_by_tenant()
    assert usage == {"t1": 165, "t2": 10}
    assert truncated is False


def test_tokens_by_tenant_groups_unattributed_separately(tmp_path) -> None:
    """没有归属的调用归到 `(未归属)`，**不能摊到某个租户头上**。"""
    log = LLMAuditLog(str(tmp_path))
    _record(log, tokens_in=1, tokens_out=1)
    usage, _ = log.tokens_by_tenant()
    assert usage == {"(未归属)": 2}


def test_tokens_by_tenant_filters_by_since(tmp_path) -> None:
    """★ 只统计"本月"：上个月的记录不能算进来（那是两个账期）。"""
    log = LLMAuditLog(str(tmp_path))
    with principal_scope(_principal("t1", "u1")):
        _record(log, tokens_in=999, tokens_out=1)      # 先写一条

    # 手工把它的时间改成上个月（写入用的是 datetime.now()，无时区）
    last_month = (datetime.now().replace(day=1, hour=0, minute=0, second=0,
                                         microsecond=0)
                  - timedelta(days=1)).isoformat(timespec="seconds")
    lines = log.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["ts"] = last_month
    log.path.write_text(json.dumps(row, ensure_ascii=False) + "\n",
                        encoding="utf-8")

    with principal_scope(_principal("t1", "u1")):
        _record(log, tokens_in=10, tokens_out=5)       # 本月的

    month_start = datetime.now().replace(day=1, hour=0, minute=0,
                                        second=0, microsecond=0)
    usage, _ = log.tokens_by_tenant(
        since=month_start.isoformat(timespec="seconds"))
    assert usage == {"t1": 15}, f"上月记录混进来了：{usage}"


def test_tokens_by_tenant_skips_corrupt_lines(tmp_path) -> None:
    """日志是追加写的：断电会留半行，不能让它把整页监控搞崩。"""
    log = LLMAuditLog(str(tmp_path))
    with principal_scope(_principal("t1", "u1")):
        _record(log, tokens_in=4, tokens_out=6)
    log.path.write_text(
        '{"ts":"2026-01-01T00:00:00","tenant_id":"t1"\n'      # 坏行
        + log.path.read_text(encoding="utf-8"),
        encoding="utf-8")
    usage, _ = log.tokens_by_tenant()
    assert usage == {"t1": 10}


def test_tokens_by_tenant_reports_truncation(tmp_path) -> None:
    """★ 超过统计窗口时必须**能被告知**：累计偏低就不能当账单看。"""
    log = LLMAuditLog(str(tmp_path))
    with principal_scope(_principal("t1", "u1")):
        for _ in range(6):
            _record(log, tokens_in=1, tokens_out=0)
    usage, truncated = log.tokens_by_tenant(limit=2)
    assert truncated is True, "只统计了 2 行却没报告截断"
    assert usage["t1"] == 2, "应当只累计仍在窗口内的那 2 行"

    full, full_truncated = log.tokens_by_tenant(limit=100)
    assert (full["t1"], full_truncated) == (6, False)


def test_tokens_by_tenant_missing_file_is_empty(tmp_path) -> None:
    log = LLMAuditLog(str(tmp_path / "nope"))
    usage, truncated = log.tokens_by_tenant()
    assert usage == {} and truncated is False


@pytest.mark.parametrize("limit", [1, 3])
def test_truncation_flag_is_exact(tmp_path, limit: int) -> None:
    """边界：正好等于窗口大小**不算**截断（否则界面会白报一次"偏低"）。"""
    log = LLMAuditLog(str(tmp_path))
    with principal_scope(_principal("t1", "u1")):
        for _ in range(3):
            _record(log, tokens_in=1, tokens_out=0)
    _, truncated = log.tokens_by_tenant(limit=limit)
    assert truncated is (3 > limit)
