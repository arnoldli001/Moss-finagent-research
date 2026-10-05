"""**判据：声明为私有的文件不得出现在已跟踪集合里**（2026-10-02，CHG-0159）。

## 为什么这条判据和"凭据扫描"不是同一件事

凭据扫描管的是"文件里的**内容**"；这条管的是"**文件本身**该不该出去"。

实测（2026-10-02）：**343 个文件**的路径已经命中 `.gitignore` 里的"私有"规则
（面试复盘 23 个、主线策略文档 69 个、非白名单脚本 202 个、私有 skill 副本
10 个、`web/dist-pilot/` 快照 2 个……），却依然躺在**公开**仓库里。

根因**不是**"忘了写 .gitignore"，而是 `gitignore` 的语义：

> `.gitignore` 只影响**未跟踪**文件。已经 `git add` 过的文件，之后把规则写进
> `.gitignore` 也**不会**把它撤下来 —— 它照旧被 `git status` 视为已跟踪、
> 照旧随下一次 push 出去。

仓库里那条路径早有人踩过并写进了工具注释（`scripts/release_public_snapshot.py`：
「`checkout` 会把文件写进索引，对已跟踪文件绕过 .gitignore」），但**只有注释、
没有判据** —— 于是又一次发生。这条判据把"注释"变成"提交前会红"。

## 它不是"必须删掉这 343 个文件"

它们是历史债，且部分可能是**有意**发布的（如 `web/dist-pilot` 快照）。
所以本判据只做两件事：

1. **不许再新增**：新出现的"已跟踪但被声明私有"的文件必须当场处理；
2. **基线不许腐烂**：已经撤下的条目必须从基线删掉，否则基线就成了一张
   "曾经的样子"的废纸，数字也不再有意义。

裁定（是否成批撤下这 343 个）属于人的决策，判据只负责让它可见、可追踪。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "configs" / "privacy_tracking_baseline.yaml"


def _tracked_but_ignored() -> set[str]:
    """已跟踪、但路径命中 .gitignore 私有规则的文件集合。

    `-z` 是必须的：不带它时 git 按 `core.quotePath` 给非 ASCII 路径加引号，
    中文文件名永远匹配不上自己的输入键（会得到"什么都没被忽略"的假绿灯）。
    """
    proc = subprocess.run(
        ["git", "ls-files", "-i", "-c", "--exclude-standard", "-z"],
        cwd=str(ROOT), capture_output=True, check=True)
    return {p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p}


def _baseline() -> tuple[set[str], int]:
    data = yaml.safe_load(BASELINE.read_text(encoding="utf-8")) or {}
    return set(data.get("paths") or []), int(data.get("frozen_count") or 0)


@pytest.fixture(scope="module")
def current() -> set[str]:
    if shutil.which("git") is None:
        pytest.skip("环境里没有 git —— 无法枚举已跟踪集合")
    return _tracked_but_ignored()


def test_no_new_private_files_are_tracked(current: set[str]) -> None:
    """**红线**：不得新增"按 .gitignore 本该私有、却已被跟踪"的文件。

    失败时说明你（或某个脚本）刚刚提交了一个私有文件 —— 它现在在公开仓库里。
    两条出路，都必须留下痕迹：
      ① 撤下它：`git rm --cached <路径>`（本地文件保留），然后提交；
      ② 若它**确实**该公开：把对应规则从 `.gitignore` 删掉（别让它继续
         声明私有却公开着 —— 那会让这份判据长期失真）。
    """
    froze, _ = _baseline()
    new = sorted(current - froze)
    assert not new, (
        f"新出现 {len(new)} 个'已跟踪但被 .gitignore 声明为私有'的文件：\n  "
        + "\n  ".join(new[:30])
        + f"\n\n（当前 {len(current)} 个 / 基线冻结 {len(froze)} 个）\n"
          "要么撤下（git rm --cached），要么删掉那条 .gitignore 规则。"
          "两者都不做的话，这份基线就成了'永远为真'的空断言。")


def test_baseline_does_not_rot(current: set[str]) -> None:
    """**棘轮**：基线里已经不存在于当前集合的条目必须删掉。

    没有这条，基线的数字会越来越大却越来越不描述现实 ——
    读的人会以为"还有 343 个私有文件在公开仓库里"，而实际早就撤下了。
    """
    froze, declared = _baseline()
    gone = sorted(froze - current)
    assert not gone, (
        f"基线里有 {len(gone)} 条已经不再是'已跟踪但被忽略'的状态：\n  "
        + "\n  ".join(gone[:30])
        + "\n\n请重新生成基线（这是**好事**：说明有人把它们撤下了）：\n"
          "  uv run python scripts/_gen_privacy_baseline.py --write")
    assert declared == len(froze), (
        f"基线自相矛盾：`frozen_count: {declared}` 但 `paths` 有 {len(froze)} 条。"
        f"请用生成脚本重写。")


def test_the_judge_itself_can_still_fail() -> None:
    """**自证**：这条判据必须真的能红。

    用一个**必然被 .gitignore 匹配**的假路径验证"集合判定"这件事本身没坏；
    同时确认当前集合非空（若为 0，说明 `ls-files -i` 的用法坏了 —— 那才是假绿）。
    """
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "scripts/prune_crowding.py"],
        cwd=str(ROOT), capture_output=True)
    assert proc.returncode == 0, (
        "样本路径应当被判定为忽略（`--no-index` 是必须的：默认情况下 git "
        "不会把**已跟踪**文件报成被忽略，会得到'什么都没被忽略'的假绿灯）")
    assert _tracked_but_ignored(), (
        "当前集合为空 —— 说明枚举方式已经失效（假绿），先修判据再谈结论")
