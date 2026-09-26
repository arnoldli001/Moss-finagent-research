"""值守命令的回归测试（`manage.py ensure`）。

## 为什么这个命令需要测试

2026-09-26 的事故：对外实例（8110）**被静默终止** —— 端口无监听、进程全无，
`backend.log` 尾部只到最后一个 `200 OK`，没有任何异常或退出痕迹
（强杀不走 lifespan，所以事后从日志找原因这条路本身就是断的）。

`ensure` 是应对它的一半（另一半是计划任务）：**自动恢复 + 当时就记现场**。
它的判据写错的代价是**自己制造故障**，所以逐条钉住：

1. 端口上是本项目实例 → **什么都不做**（绝大多数 tick 走这条，必须零副作用）；
2. 端口空 → 重启，并记下"最后一次活动时刻"（判断死亡时刻的唯一线索）；
3. 端口被**别的程序**占用 → **拒绝启动**并记账。
   ⚠️ 绝不能"先杀了再起"：`kill_pid_tree` 是硬杀，会打断别的业务程序。
4. **不做**"健康检查失败就重启"：`/health` 冷启动时会几十秒才回，
   把"慢"判成"死"会让值守反复重启一个正在正常工作的实例。
"""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import manage  # noqa: E402


@pytest.fixture()
def sandbox(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """把事件流水与日志路径都指到临时目录（**不许碰生产 data/run**）。"""
    monkeypatch.setattr(manage, "RUN_DIR", tmp_path)
    monkeypatch.setattr(manage, "INCIDENT_LOG", tmp_path / "incidents.jsonl")
    return tmp_path


def _args(**over) -> Namespace:
    base = dict(env="pilot", port=8110, name="backend", verbose=False)
    base.update(over)
    return Namespace(**base)


def _incidents(sandbox: Path) -> list[dict]:
    p = sandbox / "incidents.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in
            p.read_text(encoding="utf-8").splitlines() if line.strip()]


# ======================================================================
# 判据 1：健康 → 零副作用
# ======================================================================

def test_healthy_instance_is_a_noop(sandbox, monkeypatch) -> None:
    """★ 端口上是本项目实例时：不重启、**不写任何事件**。

    绝大多数 tick 都走这条路径（每分钟一次）。它一旦有副作用，
    计划任务日志会被刷屏，而且"记账"本身会淹没真正的事故记录。
    """
    monkeypatch.setattr(manage, "diagnose_port",
                        lambda port: {"occupied": True, "pid": 1234,
                                      "is_ours": True, "cmdline": "uvicorn"})
    called = []
    monkeypatch.setattr(manage, "_ensure_restart",
                        lambda **kw: called.append(kw) or 0)

    assert manage.cmd_ensure(_args()) == 0
    assert called == [], "健康时不该重启"
    assert _incidents(sandbox) == [], "健康时不该记事件"


# ======================================================================
# 判据 2：端口空 → 重启 + 记现场
# ======================================================================

def test_missing_backend_is_restarted_and_recorded(sandbox, monkeypatch) -> None:
    """★ 端口空 → 重启，并记下**最后一次活动时刻**。

    `last_activity` 取自日志文件的 mtime —— 强杀不走 lifespan，
    日志里不会有"退出"记录，这个时刻是事后判断"死在哪一刻"的唯一线索。
    """
    monkeypatch.setattr(manage, "diagnose_port",
                        lambda port: {"occupied": False, "pid": None,
                                      "is_ours": False})
    log = sandbox / "backend.log"
    log.write_text("最后一个 200 OK\n", encoding="utf-8")
    monkeypatch.setattr(manage, "_ensure_restart", lambda **kw: 0)

    assert manage.cmd_ensure(_args()) == 0

    events = [e["event"] for e in _incidents(sandbox)]
    assert events == ["restart", "restart_ok"], events
    first = _incidents(sandbox)[0]
    assert first["port"] == 8110
    assert first["env"] == "pilot"
    assert first["reason"], "必须写清为什么重启（端口无监听）"
    assert first["last_activity"], (
        "必须记下最后一次活动时刻 —— 强杀不留日志，这是唯一的死亡时刻线索")


def test_restart_failure_is_recorded(sandbox, monkeypatch) -> None:
    """重启失败也要记账（否则"值守跑了但没起来"完全不可见）。"""
    monkeypatch.setattr(manage, "diagnose_port",
                        lambda port: {"occupied": False, "pid": None,
                                      "is_ours": False})
    monkeypatch.setattr(manage, "_ensure_restart", lambda **kw: 1)

    assert manage.cmd_ensure(_args()) == 1
    assert [e["event"] for e in _incidents(sandbox)] == [
        "restart", "restart_failed"]


# ======================================================================
# 判据 3：别人的端口 → 拒绝
# ======================================================================

def test_foreign_port_owner_is_refused_not_killed(sandbox, monkeypatch) -> None:
    """★★ 端口被**别的程序**占用时：拒绝启动，**绝不杀**。

    这是本命令最危险的一条：若误判成"我的旧实例"就会 `taskkill /F`
    打断别人的业务程序。所以这里断言两件事：
      · 返回 2（明确的"拒绝"而不是"失败"）；
      · **一次 restart 都没发起**。
    """
    monkeypatch.setattr(manage, "diagnose_port",
                        lambda port: {"occupied": True, "pid": 999,
                                      "is_ours": False,
                                      "cmdline": "some-other-app.exe"})
    called = []
    monkeypatch.setattr(manage, "_ensure_restart",
                        lambda **kw: called.append(kw) or 0)
    killed = []
    monkeypatch.setattr(manage, "kill_pid_tree",
                        lambda pid: killed.append(pid) or True)

    assert manage.cmd_ensure(_args()) == 2
    assert called == [], "别人的端口不许启动我们自己的实例"
    assert killed == [], "★ 绝不许杀掉占用端口的其他程序"
    assert [e["event"] for e in _incidents(sandbox)] == ["foreign_port_owner"]


# ======================================================================
# 判据 4：不把"慢"判成"死"
# ======================================================================

def test_health_is_not_used_as_the_liveness_judgement() -> None:
    """★ 判据只看**端口有没有人听**，不看 `/health` 的响应快慢。

    `/health` 在冷启动、仓库统计生成时会几十秒才回（`cmd_status` 的注释
    记过这条实测）。拿它当存活判据会让值守反复重启一个正在正常工作的实例
    —— 把可用性问题变成自己制造的故障。

    这条用源码断言（与 `test_manage_process_lifecycle` 同一手法）：
    在"端口空 → 重启"那条分支之前，不许出现对 `/health` 的调用。
    """
    import inspect

    body = inspect.getsource(manage.cmd_ensure)
    # 只允许注释里提到 /health（解释为什么不用它）
    code_lines = [
        ln.strip() for ln in body.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    # 去掉 docstring 后再找
    doc_start = code.find('"""')
    if doc_start >= 0:
        doc_end = code.find('"""', doc_start + 3)
        code = code[:doc_start] + code[doc_end + 3:]
    assert "api/v1/health" not in code, (
        "值守不许拿 /health 当存活判据（慢启动会被误判成死亡）")
    assert "diagnose_port" in code, "存活判据应当是端口占用诊断"


def test_replace_flag_is_never_used_by_watchdog() -> None:
    """★ 值守重启**绝不传 `--replace`**。

    它按命令行枚举本项目**全部**后端进程，会把正在跑的另一个实例
    （比如 dev 8100）一起停掉 —— 那是"守一个实例、杀掉另一个"。
    本函数只在端口空时被调用，本来也不需要 replace。
    """
    import inspect

    body = inspect.getsource(manage._ensure_restart)
    assert "replace=False" in body, "必须显式关掉 replace"
    assert "replace=True" not in body
