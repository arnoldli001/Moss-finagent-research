"""`CHG-0243`：`--install` 必须**真的能跑**（dry-run 判据）。

## 这个文件在防什么

`CHG-0239` 把 `install()` 的 `ExecutionTimeLimit` 从写死一小时改成按间隔算，
我只把字符串里的 `{limit_sec}` 换掉了，**却没定义 `limit_sec`** ⇒
`--install` 每一次都 `NameError`。

★ 它**藏了两天没被发现**，因为：
  · `execution_time_limit_sec()` 的**纯函数判据**是绿的（函数本身没错）；
  · `--self-test` 是绿的（它不碰 `install()`）；
  · 只有在"**真要重建计划任务**"时才会执行到那一行 —— 2026-10-11 电脑重启、
    三个任务被删、必须重建时才发现 `--install` 是坏的。

⇒ 这就是本项目反复记的那条：**"改了 ≠ 生效了"**，而"生效"要靠**能跑到那条路径**的判据。
`install(dry_run=True)` 恰好是**无副作用但会走完整条拼装路径**的入口
（它只打印将要执行的注册脚本），所以用它当判据：一个 `NameError` 立刻变红。

跑法：
    uv run python -m pytest tests/unit/test_autostart_install_wiring.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import moss_autostart as m  # noqa: E402


def test_install_dry_run_runs_without_error(capsys) -> None:
    """★ 回归护栏：`install(dry_run=True)` 必须能跑完并返回 0。

    修复前这里是 `NameError: name 'limit_sec' is not defined`（`CHG-0243`）。
    """
    rc = m.install(dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0, f"dry-run 应返回 0，实得 {rc}"
    # 每个**可代装**的任务都必须被打印到（漏装一个不许静默通过）
    for spec in m.REQUIRED_TASKS:
        if spec.installable:
            assert f"REGISTERED {spec.name}" in out, f"{spec.name} 没有出现在注册脚本里"
    # 不可代装的只核对（`MossFrpEnsure` 必须在输出里被明确跳过一次）
    for spec in m.REQUIRED_TASKS:
        if not spec.installable:
            assert spec.name in out, f"{spec.name} 连「只核对」的提示都没有"


def test_dry_run_uses_the_computed_execution_time_limit(capsys) -> None:
    """★ 接线判据：注册脚本里的秒数必须**等于** `execution_time_limit_sec()` 的返回值。

    只测纯函数会漏掉"字符串里写的是别的变量"这类接线错误 —— 而 `CHG-0243` 正是这一类。
    """
    m.install(dry_run=True)
    out = capsys.readouterr().out
    for spec in m.REQUIRED_TASKS:
        if not spec.installable:
            continue
        want = m.execution_time_limit_sec(spec)
        # 每个任务各自那段脚本里都要出现它的秒数
        block = out.split(f"— {spec.name}（dry-run）")[1].split("— ")[0]
        assert f"ExecutionTimeLimit (New-TimeSpan -Seconds {want})" in block, \
            f"{spec.name} 的注册脚本没有用计算出来的 {want}s：{block[:200]}"
        # 60s 一轮的值守必须是 3 分钟，**不能**再出现"1 小时"
        assert "Seconds 3600" not in block, f"{spec.name} 又回到了一小时"


def test_dry_run_does_not_touch_the_system(capsys) -> None:
    """dry-run 不许有任何副作用：输出里只能有 `Register-ScheduledTask` 的**文本**。

    它出现在脚本字符串里是正常的（那是"将要执行"的内容），但函数本身不得调用
    `_ps()`（真跑 PowerShell）—— 用 monkeypatch 把 `_ps` 换成"一旦被调用就失败"。
    """
    def boom(*_a, **_kw):  # noqa: ANN002, ANN003
        raise AssertionError("dry-run 居然真的执行了 PowerShell（有副作用）")

    original = m._ps
    m._ps = boom
    try:
        assert m.install(dry_run=True) == 0
    finally:
        m._ps = original
    capsys.readouterr()
