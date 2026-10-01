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


# ======================================================================
# 判据 5：不把"忙"判成"死"（P15，2026-09-27 实测对外中断）
# ======================================================================

def test_listening_socket_wins_over_slow_connect(monkeypatch) -> None:
    """★ 只要有人持有 LISTEN 套接字，就算"活着" —— 与它答不答无关。

    P15 事故：`diagnose_port` 原来第一道门是 `port_open(timeout=0.4)`，
    0.4 秒连不上就返回 `occupied: False`。后端忙 >0.4s（冷启动 / 进程内调度
    跑数据作业 / 多实例争 SQLite 单写者）→ 被判"进程已消失" → `ensure` 再起一个
    → **Windows 允许重复绑定同一 LISTEN 端口** → 两个实例争单写者 → 更慢
    → 下一次探测更容易超时 → 正反馈成风暴。

    外部表现是"后端静默死亡：无崩溃日志、不走 lifespan、`in_job: none`"，
    而真相是**没有任何进程被杀**。

    本用例把 connect 固定成"永远失败"（模拟忙到不应答），
    期望结论仍是"有人在、而且是我们的实例"。
    """
    monkeypatch.setattr(manage, "find_listening_pid", lambda port: 4321)
    monkeypatch.setattr(manage, "get_cmdline",
                        lambda pid: "uvicorn src.api.main:app")
    monkeypatch.setattr(manage, "health_signature", lambda port: None)
    monkeypatch.setattr(manage, "port_open", lambda *a, **k: False)

    info = manage.diagnose_port(8110)
    assert info["occupied"] is True, (
        "★ 有 LISTEN 套接字就是活着 —— 判成空闲会让 ensure 复制实例（P15）")
    assert info["is_ours"] is True
    assert info["pid"] == 4321


def test_single_connect_timeout_cannot_decide_dead(monkeypatch) -> None:
    """★ 单次 connect 超时**不足以**判"没人听"：必须稳定复现才算。

    netstat 看不到 LISTEN 时才退到 connect 复核，且要求连续多次都失败。
    这里让第 2 次就成功 —— 期望结论是"有人在"，而不是"空闲、可以重启"。
    """
    monkeypatch.setattr(manage, "find_listening_pid", lambda port: None)
    monkeypatch.setattr(manage, "health_signature", lambda port: None)
    calls = {"n": 0}

    def flaky(*_a, **_k):
        calls["n"] += 1
        return calls["n"] >= 2          # 第 2 次起可连

    monkeypatch.setattr(manage, "port_open", flaky)

    info = manage.diagnose_port(8110)
    assert info["occupied"] is True, "复核期间连上了 → 绝不能判成空闲"
    assert calls["n"] >= 2, "必须重试；一次 0.4 秒超时不许作为结论"


def test_truly_free_port_is_reported_free(monkeypatch) -> None:
    """真的没人听时才允许判空闲（`ensure` 据此重启，这是它的正常工作路径）。"""
    monkeypatch.setattr(manage, "find_listening_pid", lambda port: None)
    monkeypatch.setattr(manage, "port_open", lambda *a, **k: False)

    info = manage.diagnose_port(8110)
    assert info["occupied"] is False
    assert info["is_ours"] is False


def test_port_probe_parameters_cannot_regress_to_one_shot() -> None:
    """护栏：探测参数不许被改回"一次 0.4 秒就下结论"（P15 的成因）。"""
    assert manage.PORT_PROBE_ATTEMPTS >= 2, "必须重试"
    assert manage.PORT_PROBE_TIMEOUT >= 0.8, "单次超时不能太短"
    assert manage._connect_fails_consistently.__doc__, "复核函数必须有说明"
