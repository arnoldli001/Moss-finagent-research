"""值守的「正在启动」宽限 + `--replace` 运行期拒绝（`CHG-0145`）。

## 这组判据对应的真实事故（2026-09-30 16:34–16:41）

pilot 每次被拉起后 **~70 秒**就"端口无监听"，值守再拉，**公网 502 持续 7 分钟**；
现场抓到 **4 个 `--port 8110` 进程 = 2 个并发实例**（各自重复跑启动预热、
互抢 SQLite `database is locked`）。而**停掉值守、清掉残留、单实例启动
只用了 2 秒**就绑定端口 —— 也就是说：故障不是"启动慢"，是**值守把
"正在启动"误判成"已经死了"**。

判据分两层：
  · **纯函数层**（`starting_instance` / `replace_refusal_reason`）—— 快、确定；
  · **入口层**（`cmd_ensure` / `cmd_start` 的行为）—— 走真函数、只替身进程表，
    因为"改了函数"不等于"值守真的不再多拉一个实例"。
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

import manage  # noqa: E402


@pytest.fixture()
def sandbox(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """事件流水与日志路径都指到临时目录（**不许碰生产 data/run**）。"""
    monkeypatch.setattr(manage, "RUN_DIR", tmp_path)
    monkeypatch.setattr(manage, "INCIDENT_LOG", tmp_path / "incidents.jsonl")
    return tmp_path


def _args(**over) -> Namespace:
    base = dict(env="pilot", port=8110, name="backend", verbose=False)
    base.update(over)
    return Namespace(**base)


def _incidents(sandbox: Path) -> list[dict]:
    path = sandbox / "incidents.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _port_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manage, "diagnose_port",
                        lambda port: {"occupied": False, "pid": None,
                                      "is_ours": False, "cmdline": ""})


# ======================================================================
# 纯函数层
# ======================================================================

def test_starting_instance_picks_the_youngest_within_grace() -> None:
    """宽限内挑**最年轻**的那个（两个都在启动时，报最近起来的那个）。"""
    found = manage.starting_instance([(111, 300.0), (222, 12.5), (333, 299.0)])
    assert found == (222, 12.5)


def test_starting_instance_is_none_when_all_are_old() -> None:
    """全都超出宽限 ⇒ 不拦（真死了就该重启，宽限不能变成"永远不重启"）。"""
    assert manage.starting_instance([(111, 900.0), (222, 601.0)]) is None
    assert manage.starting_instance([]) is None


def test_starting_instance_ignores_negative_age() -> None:
    """时钟漂移/解析异常给出负数时按"不可信"处理，不当作刚启动。"""
    assert manage.starting_instance([(111, -5.0)]) is None


def test_replace_refused_when_other_instances_are_running() -> None:
    """★ 别的实例在跑 ⇒ 必须拒绝，且**错误消息里带上正确步骤**（拒绝要给出路）。"""
    reason = manage.replace_refusal_reason([4321, 8765])
    assert reason, "端口外还有实例时必须拒绝 --replace"
    assert "4321" in reason
    assert "MossPilotWatchdog" in reason and "manage.py start" in reason


def test_replace_allowed_only_when_this_is_the_only_instance() -> None:
    """只有本端口这一个实例时才允许（这时 `--replace` 不会伤到别人）。"""
    assert manage.replace_refusal_reason([]) == ""


# ======================================================================
# 入口层：cmd_ensure 真的不再多拉一个
# ======================================================================

def test_ensure_skips_restart_while_an_instance_is_booting(
        sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 端口空 + 有实例正在启动 ⇒ **不重启**，并记一条 `startup_grace_skip`。

    这就是 2026-09-30 事故缺的那一层：端口空是启动期的**正常状态**。
    """
    _port_free(monkeypatch)
    monkeypatch.setattr(manage, "our_backend_processes", lambda: [(4242, 30.0)])
    calls: list[dict] = []
    monkeypatch.setattr(manage, "_ensure_restart",
                        lambda **kw: calls.append(kw) or 0)

    assert manage.cmd_ensure(_args()) == 0
    assert calls == [], "正在启动时不该再拉一个实例"
    events = [item["event"] for item in _incidents(sandbox)]
    assert events == ["startup_grace_skip"], events
    assert _incidents(sandbox)[0]["pid"] == 4242


def test_ensure_restarts_after_grace_expires(
        sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    """宽限过期 ⇒ 照常重启（反向判据：别把"启动中"变成"永不重启"）。"""
    _port_free(monkeypatch)
    monkeypatch.setattr(manage, "our_backend_processes", lambda: [(4242, 900.0)])
    calls: list[dict] = []
    monkeypatch.setattr(manage, "_ensure_restart",
                        lambda **kw: calls.append(kw) or 0)

    assert manage.cmd_ensure(_args()) == 0
    assert len(calls) == 1, "超期未启动的实例必须被重启"
    events = [item["event"] for item in _incidents(sandbox)]
    assert "restart" in events and "startup_grace_skip" not in events


def test_ensure_restarts_when_process_probe_returns_nothing(
        sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    """探测拿不到进程表（返回空）⇒ 照常重启。

    ★ 这条是**故障方向**判据：探测失败时若也走宽限，值守会永久停止工作
    （"静默失效"），而它正是我们唯一的运行期值守。
    """
    _port_free(monkeypatch)
    monkeypatch.setattr(manage, "our_backend_processes", lambda: [])
    calls: list[dict] = []
    monkeypatch.setattr(manage, "_ensure_restart",
                        lambda **kw: calls.append(kw) or 0)

    assert manage.cmd_ensure(_args()) == 0
    assert len(calls) == 1
