"""★ 动态注册的作业必须真的被注册：**分发分支 ↔ 作业登记表** 双向对齐。

## 这条判据的来历（一次真实的"我差点报错案"）

我在核查"两个搜索源发布得出去吗"时注意到：`jobs.py` 里有
`if spec.kind == "gap_drain":` 的分发分支，而**静态 import** `JOB_REGISTRY`
只看到 **43** 个作业、里面**没有** `gap_drain` ⇒ 我一度判它"死分支、
缺口队列从来没被调度过"。

**那个结论是错的**，而且错在一个很容易再犯的地方：

> `gap_drain` 等 4 个作业由 `catalog_jobs.install_catalog_jobs()`
> **在应用启动时动态注册** —— 静态 import 看不到它们。
> 实测：`install_catalog_jobs()` 之后 `JOB_REGISTRY` 是 **49** 个
> （与 pilot 启动横幅的「49/49 个作业」逐字吻合）。

## 那这条判据守什么

守住那个**形状**本身 —— 它确实是一种真实且静默的失效：

| 形状 | 后果 |
|---|---|
| 有分发分支、但**没有任何作业登记**这个 kind | **死分支**：那个作业永远不会被调度，而**不报错**（`_tick()` 只遍历 `JOB_REGISTRY`） |
| 登记了 kind、但**分发链没有分支** | 每次触发都记"未知作业类型"失败（`AGENTS.md` 记着这个坑） |

所以我**误判的那件事**如果真发生，这条判据会**当场抓到**。
两个方向都要断言，并且**必须用运行时视图**（先调 `install_catalog_jobs()`），
否则判据自己就会重犯我那个错。

★ 自证：喂一个"有分支但没登记"的已知坏输入，必须被抓到。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.scheduler.catalog_jobs import install_catalog_jobs


#: ★ **条件注册**的 kind —— 它们**不是死分支**，只是"按需"注册，
#: 所以在静态/常规注册表里看不到。
#:
#: ⚠️ 每个条目**必须写清"谁在什么时候注册它"**。只写"已知"是不合格的豁免 ——
#: 下一个人会以为那是死代码，把 `jobs.py` 里的分支删掉（那就真成了缺陷）。
CONDITIONALLY_REGISTERED: dict[str, str] = {
    "dynamic_collection": (
        "由 A19 自修复**按需**注册：`registry.register_dynamic_job()` 写 "
        "`data/dynamic_connectors/_schedule.json`；进程启动时 "
        "`load_dynamic_jobs()`（被 `src/api/runtime.py:335` 调用）恢复。"
        "实测该清单**当前不存在** ⇒ **至今没有产生过任何动态作业** "
        "（属「自愈=一次性」那条已登记缺口，不是本判据要挡的缺陷）。"
        "**不要**因此删掉 `jobs.py:439` 的分支 —— 分支是对的，缺的是上游产物。"
    ),
}


def _dispatch_kinds(src: str) -> set[str]:
    """从 `jobs.py` 源码里抠出所有 `spec.kind == "<x>"` 的 `<x>`（语法树，非正则）。"""
    tree = ast.parse(src)
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Attribute) and left.attr == "kind"):
            continue
        for c in node.comparators:
            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                out.add(c.value)
    return out


@pytest.fixture()
def runtime_kinds() -> set[str]:
    """**运行时**登记表（含动态注册）—— 静态 import 会漏掉 4 个作业。"""
    install_catalog_jobs()          #: 幂等：重复调用不会重复注册
    from src.scheduler.registry import JOB_REGISTRY

    return {str(s.kind) for s in JOB_REGISTRY.values()}


def test_guard_selfproof_detects_orphan_dispatch_branch() -> None:
    """★ 自证：一个"有分支、没人登记"的已知坏输入必须被判红。

    没有这一条，下面的判据可能恒绿（比如 AST 取错了节点就永远拿到空集）。
    """
    fake = '''
if spec.kind == "registered_kind":
    pass
if spec.kind == "orphan_kind":
    pass
'''
    got = _dispatch_kinds(fake)
    assert got == {"registered_kind", "orphan_kind"}, f"AST 没取全：{got}"
    registered = {"registered_kind"}
    assert got - registered == {"orphan_kind"}, "孤儿分支没被算出来（判据恒绿）"


def test_every_dispatch_branch_has_a_registered_job(runtime_kinds: set[str]) -> None:
    """★ 每个分发分支都必须有作业登记它（否则是**永远不跑的死分支**）。

    `CONDITIONALLY_REGISTERED` 里的 kind 例外，但**例外本身要写清机制**
    （见那个常量的注释）—— 允许"按需注册"，不允许"没人管"。
    """
    kinds = _dispatch_kinds(Path("src/scheduler/jobs.py").read_text(encoding="utf-8"))
    assert kinds, "一个分发分支都没解析到 —— AST 判据失效（判据恒绿）"
    orphans = sorted(kinds - runtime_kinds - set(CONDITIONALLY_REGISTERED))
    assert not orphans, (
        "以下 kind 在 `jobs.py` 里有分发分支，但**既没有作业登记、也不在条件注册名单里** ⇒ "
        "那些分支永远不会被触发（而 `_tick()` 只遍历 JOB_REGISTRY，**不报错**）：\n  "
        + "\n  ".join(orphans)
        + "\n（若它本该由动态注册提供，请确认本判据用的运行时视图 —— "
          "`install_catalog_jobs()` 之后 JOB_REGISTRY 才有那 4 个；"
          "若它确实按需注册，请加进 CONDITIONALLY_REGISTERED **并写清谁注册它**）")


def test_job_kind_literal_has_no_unused_entries(runtime_kinds: set[str]) -> None:
    """反向：`JobKind` 字面量里的取值**应当**都真被用到。

    留一个没人用的 kind 不会有害，但它会让"读 JobKind 猜系统能干什么"的人猜错。
    允许两类例外，且**都必须显式登记 + 写明理由**（`AGENTS.md`：豁免要带过期检查）：
      · `CONDITIONALLY_REGISTERED` —— 按需注册的（机制见该常量）；
      · `KNOWN_UNUSED` —— 声明了但暂未接线的。
    """
    import typing

    from src.scheduler import registry as R

    declared: set[str] = {a for a in typing.get_args(R.JobKind) if isinstance(a, str)}
    unused = sorted(declared - runtime_kinds - set(CONDITIONALLY_REGISTERED))
    #: 目前为空；将来若有"声明了但暂未接线"的 kind，**在这里登记并写理由**。
    KNOWN_UNUSED: set[str] = set()
    assert set(unused) <= KNOWN_UNUSED, (
        f"JobKind 里声明了但没有任何作业使用的取值：{unused} —— "
        "要么给它接线，要么加进 CONDITIONALLY_REGISTERED / KNOWN_UNUSED 并写明理由")


def test_install_catalog_jobs_is_idempotent(runtime_kinds: set[str]) -> None:
    """动态注册必须**幂等**：重复调用不该把作业表越滚越大。"""
    from src.scheduler.registry import JOB_REGISTRY

    before = len(JOB_REGISTRY)
    install_catalog_jobs()
    install_catalog_jobs()
    assert len(JOB_REGISTRY) == before, (
        f"重复 install_catalog_jobs() 让作业表从 {before} 涨到 "
        f"{len(JOB_REGISTRY)} —— 启动流程重入会造出重复作业")
