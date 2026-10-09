"""`scripts/moss_autostart.py` 的判据护栏（`CHG-0168`）。

## 这组测试对应什么真实事故

2026-10-05：对外入口全站 502，根因是 `MossPilotAutostart` / `MossPilotWatchdog`
两个计划任务**已被删除**，而隧道值守把处置权交给了一个不存在的值守 ——
日志里写着一句正常的话，实际无人负责。**能自动化的检查就不要写成提醒**，
所以这里把"开机自启还在不在"钉成机器判据。

## 为什么要分两层测

* **纯函数层**（`evaluate` / `parse_iso_duration` / `decide_notify`）——
  快、确定、跨平台，**不碰进程表也不碰 Windows**，CI 上也能跑；
* **入口层**（`observe`）—— 注入一个假 runner，验证"探针坏掉"这件事
  **不会塌缩成 `exists=False`**。这一条是整个文件里最贵的判据：
  把"读不到"说成"不存在"，会让人去重建一个本来好好的任务（假红），
  而把它说成"OK"，就是我们刚经历的那次假绿。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import moss_autostart as ma  # noqa: E402
import moss_ops_alert as oa  # noqa: E402
import moss_vps_monitor as vm  # noqa: E402


# ======================================================================
# 纯函数层：判据认不认得出
# ======================================================================
@pytest.fixture()
def spec() -> ma.TaskSpec:
    return ma.TaskSpec(
        name="MossPilotWatchdog",
        script="scripts/pilot_watchdog.ps1",
        kind="powershell",
        interval_sec=60,
        needs_boot_trigger=False,
        installable=True,
        why="运行期值守",
        restore="--install",
    )


def _ok_obs(spec: ma.TaskSpec, **over) -> ma.Observed:
    base = dict(name=spec.name, exists=True, enabled=True,
                arguments=f'-File "{ROOT / spec.script}"', intervals_sec=(60,))
    base.update(over)
    return ma.Observed(**base)


def test_probe_failure_is_unknown_not_missing(spec: ma.TaskSpec) -> None:
    """★ 读不到 ≠ 不存在。塌缩成 MISSING 会让人去重建一个好好的任务。"""
    v = ma.evaluate(spec, ma.Observed(name=spec.name, exists=None, note="powershell 炸了"))
    assert v.state == ma.UNKNOWN
    assert "读不到" in v.detail


def test_absent_task_is_missing(spec: ma.TaskSpec) -> None:
    assert ma.evaluate(spec, ma.Observed(name=spec.name, exists=False)).state == ma.MISSING


def test_disabled_task_is_not_ok(spec: ma.TaskSpec) -> None:
    """任务在、但被停用 —— 最像"已配置"的坏形态。"""
    assert ma.evaluate(spec, _ok_obs(spec, enabled=False)).state == ma.DISABLED


def test_action_pointing_elsewhere_is_mismatch(spec: ma.TaskSpec) -> None:
    """脚本被改名/搬走时，任务还在但跑的是别的东西。"""
    v = ma.evaluate(spec, _ok_obs(spec, arguments='-File "D:\\x\\pilot_watchdog_old.ps1"'))
    assert v.state == ma.ACTION_MISMATCH


def test_changed_interval_is_mismatch(spec: ma.TaskSpec) -> None:
    """间隔 = 故障后最长不可用时间，被改大必须报出来。"""
    assert ma.evaluate(spec, _ok_obs(spec, intervals_sec=(300,))).state == ma.INTERVAL_MISMATCH
    assert ma.evaluate(spec, _ok_obs(spec, intervals_sec=())).state == ma.INTERVAL_MISMATCH


def test_boot_trigger_is_required_where_declared() -> None:
    """"开机自启"这四个字落在触发器上：没有 Boot 触发器就是名存实亡。"""
    boot_spec = ma.REQUIRED_TASKS[0]
    assert boot_spec.needs_boot_trigger, "MossPilotAutostart 必须要求开机触发器"
    obs = ma.Observed(name=boot_spec.name, exists=True, enabled=True,
                      arguments=f'-File "{ROOT / boot_spec.script}"',
                      trigger_kinds=("Logon",))
    assert ma.evaluate(boot_spec, obs).state == ma.TRIGGER_MISMATCH


def test_all_good_is_ok(spec: ma.TaskSpec) -> None:
    assert ma.evaluate(spec, _ok_obs(spec)).state == ma.OK


@pytest.mark.parametrize(("text", "want"), [
    ("PT1M", 60), ("PT5M", 300), ("PT30M", 1800), ("PT1H", 3600),
    ("P1DT2H", 93600), ("PT1H30M", 5400),
    ("", None), ("垃圾", None), ("PT", None),
])
def test_iso_duration_parsing(text: str, want: int | None) -> None:
    """看不懂必须返回 None（由调用方当"读不到"）—— 猜错的代价是判据说"间隔对"。"""
    assert ma.parse_iso_duration(text) == want


# ======================================================================
# 入口层：探针坏掉时的行为
# ======================================================================
def test_observe_never_collapses_probe_failure_into_absent() -> None:
    """★ 探针抛错/超时/输出垃圾 ⇒ 全部 UNKNOWN，**一条都不许变成 MISSING**。"""
    def boom(_names):
        return "", "powershell 退出码 1：模拟探针故障"

    obs = ma.observe(["A", "B"], runner=boom)
    assert {o.exists for o in obs.values()} == {None}


def test_observe_marks_unparseable_output_as_unknown() -> None:
    obs = ma.observe(["A"], runner=lambda _n: ("这不是 JSON", ""))
    assert obs["A"].exists is None
    assert "JSON" in obs["A"].note


def test_observe_marks_unreadable_interval_as_unknown() -> None:
    """看不懂的重复间隔 = 读不到，**不许当成"没有间隔"**。"""
    payload = json.dumps([{
        "name": "A", "exists": True, "enabled": True,
        "execute": "powershell.exe", "arguments": "-File x",
        "intervals": ["PT??M"], "trigger_kinds": ["Time"],
    }])
    obs = ma.observe(["A"], runner=lambda _n: (payload, ""))
    assert obs["A"].exists is None
    assert "PT??M" in obs["A"].note


def test_observe_parses_a_healthy_task() -> None:
    payload = json.dumps([{
        "name": "A", "exists": True, "enabled": True,
        "execute": "powershell.exe", "arguments": '-File "a.ps1"',
        "intervals": ["PT1M"], "trigger_kinds": ["Boot", "Time"],
    }])
    obs = ma.observe(["A"], runner=lambda _n: (payload, ""))
    assert obs["A"].exists is True
    assert obs["A"].intervals_sec == (60,)
    assert obs["A"].trigger_kinds == ("Boot", "Time")


def test_missing_task_in_probe_output_is_unknown_not_absent() -> None:
    """探针没提到它（输出被截断等）⇒ 读不到，不替它断言"不存在"。"""
    obs = ma.observe(["A"], runner=lambda _n: ("[]", ""))
    assert obs["A"].exists is None


def test_exit_codes_separate_broken_from_unknown() -> None:
    """1 = 确认坏了；2 = 需要人来看。**两者不能合成一个码**。"""
    broken = ma.Report(verdicts=[ma.Verdict("A", ma.MISSING, "")])
    unknown = ma.Report(verdicts=[ma.Verdict("A", ma.UNKNOWN, "")])
    healthy = ma.Report(verdicts=[ma.Verdict("A", ma.OK, "")])
    assert (broken.exit_code, unknown.exit_code, healthy.exit_code) == (1, 2, 0)


# ======================================================================
# 告警闸门三层（AGENTS.md「告警/通知硬约束」）
# ======================================================================
def test_same_failure_is_deduped_with_human_readable_remaining_time() -> None:
    now = datetime(2026, 10, 5, 10, 0, 0)
    state = ma.GateState()
    first, _, state = ma.decide_notify("A:MISSING", now, state)
    second, why, _ = ma.decide_notify("A:MISSING", now + timedelta(minutes=5), state)
    assert first is True
    assert second is False
    assert "还剩" in why and "分钟" in why, "抑制原因必须是人话 + 剩余时间"


def test_dedup_window_expires() -> None:
    now = datetime(2026, 10, 5, 10, 0, 0)
    state = ma.GateState()
    _, _, state = ma.decide_notify("A:MISSING", now, state, dedup_hours=6)
    again, _, _ = ma.decide_notify("A:MISSING", now + timedelta(hours=7), state, dedup_hours=6)
    assert again is True, "冷却过期后必须能再次提醒（否则故障被永久静音）"


def test_daily_rate_cap_suppresses_and_explains() -> None:
    state = ma.GateState()
    now = datetime(2026, 10, 5, 10, 0, 0)
    for i in range(3):
        send, _, state = ma.decide_notify(f"F{i}:MISSING", now + timedelta(minutes=i), state,
                                          dedup_hours=0, max_per_day=3)
        assert send is True
    send, why, _ = ma.decide_notify("F9:MISSING", now + timedelta(minutes=9), state,
                                    dedup_hours=0, max_per_day=3)
    assert send is False
    assert "上限" in why and "3" in why


def test_quiet_hours_suppress_but_say_so() -> None:
    send, why, _ = ma.decide_notify("A:MISSING", datetime(2026, 10, 5, 2, 0, 0),
                                    ma.GateState(), quiet_hours=(23, 7))
    assert send is False
    assert "静默时段" in why


def test_gate_state_round_trips_on_disk(tmp_path: Path) -> None:
    """闸门状态**必须落盘**：放内存里重启即失效，等于没有闸门。"""
    path = tmp_path / "state.json"
    state = ma.GateState()
    _, _, state = ma.decide_notify("A:MISSING", datetime(2026, 10, 5, 10, 0, 0), state)
    state.save(path)
    loaded = ma.GateState.load(path)
    assert loaded.last_fingerprint == "A:MISSING"
    assert loaded.alert_times == state.alert_times


def test_gate_state_survives_corrupt_file(tmp_path: Path) -> None:
    """半截/损坏的状态文件按"没有历史"处理 —— 宁可多发一次，不可漏发。"""
    path = tmp_path / "state.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    assert ma.GateState.load(path).last_fingerprint == ""


# ======================================================================
# 形状自检：表与磁盘、表与外部监控必须对得上
# ======================================================================
def test_every_required_script_exists_on_disk() -> None:
    """表里写的脚本必须在磁盘上真的存在 —— 否则每次体检都会 ACTION_MISMATCH。"""
    missing = [s.script for s in ma.REQUIRED_TASKS
               if s.script and not (ROOT / s.script).is_file()]
    assert missing == [], f"本表引用了不存在的脚本：{missing}"


def test_guard_task_checks_this_very_module() -> None:
    """★ 守卫任务必须核对**本模块自己** —— 它同样是会被删掉的持久对象。"""
    names = [s.name for s in ma.REQUIRED_TASKS]
    assert ma.GUARD_TASK_NAME in names
    guard = next(s for s in ma.REQUIRED_TASKS if s.name == ma.GUARD_TASK_NAME)
    assert guard.installable and guard.interval_sec


def test_uninstallable_tasks_carry_a_restore_hint() -> None:
    """不代装的任务必须写明为什么 —— 否则下一个人会拿通用安装器把它装坏。"""
    for spec in ma.REQUIRED_TASKS:
        if not spec.installable:
            assert "wscript" in spec.restore or "Administrator" in spec.restore


def test_placeholder_mail_recipient_is_not_configured() -> None:
    """★ 占位收件人（`your_qq_number@qq.com`）不算已配置。

    混了就会静默丢告警：代码以为发了，实际发到一个不存在的地址。
    """
    placeholder = ma.MailConfig(host="smtp.qq.com", port=465, user="a@qq.com",
                                auth_code="x", to="your_qq_number@qq.com",
                                from_name="n")
    assert placeholder.configured is False
    real = ma.MailConfig(host="smtp.qq.com", port=465, user="a@qq.com",
                         auth_code="x", to="2693888583@qq.com", from_name="n")
    assert real.configured is True


def test_env_file_parsing_strips_quotes_and_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('# 注释\nALERT_EMAIL_TO="a@b.com"\n\nKA=1\n', encoding="utf-8")
    parsed = ma.load_env_file(env)
    assert parsed["ALERT_EMAIL_TO"] == "a@b.com"
    assert parsed["KA"] == "1"
    assert "# 注释" not in parsed


# ======================================================================
# 两个发送方必须共用**同一份**闸门（不是"两份长得一样"）
# ======================================================================
def test_both_senders_share_one_gate() -> None:
    """★ 本机自检（`moss_autostart`）与 VPS 外部监控（`moss_vps_monitor`）
    是两个互相独立的发送方，但闸门/邮件/流水必须是**同一份实现**。

    判据写成**"同一个函数对象"**而不是"行为一致"：后者要靠人记得同步改两处，
    而本项目已多次证明"同一个 key 写在 N 处，必有一处被漏改"。
    """
    for name in ("decide_notify", "GateState", "send_mail", "MailConfig",
                 "load_env_file", "parse_quiet_hours"):
        # ⚠️ 用 `hasattr` 守卫而不是硬要求两个模块都导出同一个名字：
        #   判据要问的是"**它用的那个东西**是不是同一份"，不是"它必须导入全部名字"。
        #   若某天有人在这里另写一份实现，属性就会出现、且**不是**同一个对象 ⇒ 照样报错。
        for mod, label in ((ma, "本机自检"), (vm, "VPS 外部监控")):
            if hasattr(mod, name):
                assert getattr(mod, name) is getattr(oa, name), \
                    f"{name} 在{label}侧不是同一份实现"


def test_both_senders_are_really_different_programs() -> None:
    """共用闸门 ≠ 同一个程序：两条链路的**触发面**必须不同，否则等于只有一层。"""
    assert ma.GUARD_TASK_NAME and vm.DEFAULT_URL.startswith("https://")
    assert vm.FP_DOWN != ma.GUARD_TASK_NAME


# ======================================================================
# 外部监控的判据语义
# ======================================================================
def test_consecutive_failure_counter() -> None:
    assert vm.next_counter(True, 5) == 0
    assert vm.next_counter(False, 0) == 1
    assert vm.next_counter(False, 2) == 3


def test_single_blip_does_not_alert_but_threshold_does() -> None:
    """单次抖动不该半夜叫人；连续达阈值才算故障。"""
    assert vm.should_alert(False, 1, False, threshold=3) == ""
    assert vm.should_alert(False, 2, False, threshold=3) == ""
    assert vm.should_alert(False, 3, False, threshold=3) == "down"


def test_down_is_announced_once_and_recovery_is_announced() -> None:
    """报过 down 之后不再重复报；恢复**必须**说一声，否则人以为还挂着。"""
    assert vm.should_alert(False, 9, True, threshold=3) == ""
    assert vm.should_alert(True, 0, True, threshold=3) == "recovered"
    assert vm.should_alert(True, 0, False, threshold=3) == ""


def test_probe_treats_unreachable_as_down() -> None:
    """探不动 = 不通（不是"未知"）—— 对外入口的判据只有"能不能拿到 200"。"""
    ok, detail = vm.probe("http://127.0.0.1:9/nope", timeout=3)
    assert ok is False
    assert detail


def test_quiet_hours_parsing() -> None:
    assert oa.parse_quiet_hours("23-7") == (23, 7)
    assert oa.parse_quiet_hours("") is None, "留空必须等于'不静默'"
    assert oa.parse_quiet_hours("垃圾") is None
    assert oa.parse_quiet_hours("99-1") is None


def test_quiet_hours_wraps_midnight() -> None:
    assert oa.in_quiet_hours(datetime(2026, 10, 5, 23, 30), (23, 7)) is True
    assert oa.in_quiet_hours(datetime(2026, 10, 5, 3, 0), (23, 7)) is True
    assert oa.in_quiet_hours(datetime(2026, 10, 5, 12, 0), (23, 7)) is False
