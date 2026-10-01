"""★ 生产代码必须**入库**：`src/` 与 `tests/` 下不许有未跟踪文件。

## 这条判据防的是什么（本轮实测，规模很大）

`AGENTS.md`《交付完整性硬约束》记的是**反向**情形：会入库的测试引用了
**不会入库**的脚本。本轮遇到的是**正向**情形 —— **该入库的东西根本没入库**：

    实测（2026-09-30）：`src/` 下 **27 个条目**未跟踪、`tests/` 下 **57 个**，
    合计 85 个条目。它们**正在被运行中的 pilot 执行**（`src/core/intel_limits.py`
    的 10s 防撞钟、`src/core/logging_setup.py` 的 INFO 落盘、
    `src/infrastructure/catalog/network_fallback.py`、`src/domain/indicators/`、
    `src/scheduler/maintenance.py` …），却**在克隆里根本不存在**。

**为什么危险且不易察觉**：

* 本机**全绿**（6437 passed）—— 因为文件都在；
* 克隆/CI 里**不是红灯**，而是**少跑 57 个护栏文件** ⇒ 保护被静默削掉一大块；
* `--ledger` 也绿，没有任何判据会报。

也就是说：**"我交付了"与"它发布了"之间那道门，没有任何机器判据**。

## 判据为什么必须是"未跟踪"而不是"未提交"

` M`（工作区改了没提交）是**正常开发状态**，拿它报错就是自造假红；
只有 `??`（**从未 `git add` 过**）才意味着"克隆里不会有它"。
所以判据只看 `??`，并且**先自证**它既抓得住 `??`、又不误伤 ` M`。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

#: 这些目录下**不许有未跟踪文件** —— 判据是"**没有它，克隆就跑不起来/建不起来**"。
#:
#: 为什么是这几个（每一个都有"必须发布"的硬理由，不是随手加的）：
#:   · `src/`        —— 应用本体（运行中的 pilot 就在执行它们）
#:   · `tests/`      —— 护栏；不入库 ⇒ CI **少跑**而不是报红（静默削掉保护）
#:   · `configs/`    —— ★ `AGENTS.md` 明确把 `indicators.yaml` / `data_stores.yaml`
#:                      声明为**单一事实源**；没有它们，指标登记与写权限归属全没了
#:   · `web/src/`    —— 前端源码；不入库则 `manage.py build` 直接失败（import 缺文件）
#:   · `qmt/`        —— 实盘/仿真那条链的源码
#:   · `.trae/skills/` —— ★ `AGENTS.md` 的《Skills引用》**逐条点名**它们；
#:                      不入库 ⇒ 那份清单在克隆里**全是悬空引用**
#:
#: ⚠️ **故意不含**的目录（原因各不相同，都属于"要人来拍板"）：
#:   · `docs/`   —— 会话复盘/审计这类**证据**性质的东西，是否随仓库发布是政策判断；
#:   · `bin/`    —— 里面有 15 MB 的 `frpc.exe` 二进制（**不该进 git**，应记录获取方式）；
#:   · `skills/` —— 顶层那份正在被并发协作者改名/替换（在飞状态，不代改）；
#:   · 根目录的中文命名笔记/截图（个人现场记录）。
GUARDED = ("src", "tests", "configs", "web/src", "qmt", ".trae/skills")


def _untracked_entries(porcelain: str) -> list[str]:
    """从 `git status --porcelain` 输出里挑出**未跟踪**条目（`??`）。

    抽成函数是为了能**自证**：喂已知答案，确认它只认 `??`。
    """
    out: list[str] = []
    for line in porcelain.splitlines():
        if line.startswith("?? "):
            out.append(line[3:].strip())
    return out


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          check=False).stdout


@pytest.fixture()
def in_git_worktree() -> None:
    """不在 git 工作树里（例如 tarball 导出）⇒ 明确跳过，**不是**静默绿灯。"""
    if shutil.which("git") is None:
        pytest.skip("环境里没有 git 可执行文件 —— 本判据无法执行（知名降级）")
    if not Path(".git").exists() and not _git("rev-parse", "--is-inside-work-tree").strip():
        pytest.skip("不在 git 工作树里（导出包）—— 本判据无从判定（知名降级）")


def test_guard_selfproof_only_flags_untracked() -> None:
    """★ 先自证：`??` 必须抓到，` M` / `M ` / `A ` **必须不误伤**。

    没有这一条，上面那条判据可能恒绿（比如前缀判断写错就永远挑不出东西）。
    """
    sample = (
        "?? src/new_module.py\n"
        " M src/edited.py\n"
        "M  src/staged_edit.py\n"
        "A  src/newly_added.py\n"
        " D src/deleted.py\n"
        "?? tests/unit/test_new.py\n"
    )
    assert _untracked_entries(sample) == ["src/new_module.py",
                                          "tests/unit/test_new.py"], (
        "自证失败：判据要么漏了 `??`、要么把正常改动误判成未跟踪")


def test_no_untracked_production_code(in_git_worktree: None) -> None:
    """★ `src/` 与 `tests/` 下**不许有未跟踪文件**（它们不会随克隆发布）。"""
    porcelain = _git("status", "--porcelain", "--", *GUARDED)
    offenders = _untracked_entries(porcelain)
    assert not offenders, (
        "以下生产代码/护栏**从未 `git add`** ⇒ 克隆与 CI 里根本不存在"
        f"（本机全绿会造成假绿）：\n  " + "\n  ".join(offenders)
        + "\n修法：`git add " + " ".join(GUARDED) + "`"
        + "（本判据只看 `??`；` M` 是正常开发状态，不在此列）")


def test_tracked_counts_are_plausible(in_git_worktree: None) -> None:
    """兜底哨兵：两个目录的**已入库文件数**不许塌陷。

    为什么在"没有未跟踪文件"之外还要这一条：如果有人用
    `git rm -r --cached src` 之类的操作把整棵树移出索引，
    上面那条判据会**因为"没有 `??`"而通过**（文件都还在磁盘上、
    只是全变成未跟踪 —— 不，那会产生 `??`）……

    真正防的是另一种：**索引被整体重置**后 `git status` 也可能给出
    大面积的 ` D`/`??` 混合。所以这里再钉一个下界，
    阈值取得**远低于**当前实测值（src 422 / tests 319），只抓"塌陷"，
    不抓正常增减 —— 否则加删文件就会假红。
    """
    for d, floor in (("src", 300), ("tests", 200)):
        n = len([x for x in _git("ls-files", d).splitlines() if x.strip()])
        assert n >= floor, (
            f"`git ls-files {d}` 只有 {n} 个文件（下界 {floor}）—— "
            "索引像是被整体重置过；请检查是不是误用了 `git rm -r --cached`")
