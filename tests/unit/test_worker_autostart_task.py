"""worker 开机自启的**契约判据**（`CHG-0157` 收尾项）。

## 为什么这几条（每条都对应一个付过代价的坑）

1. **`MOSS_ENV` 必须在包装里显式设**：计划任务起的进程**没有**环境变量，
   `current_env()` 退化成 `dev` ⇒ **用 dev 的库去跑 pilot 的作业**。
   本项目为这个形状付过至少三次代价（`CHG-0112` 系）。
2. **动作必须走既有写法**（`powershell.exe -NoProfile -NonInteractive
   -WindowStyle Hidden -ExecutionPolicy Bypass -File`）：新造一种写法
   就多一份要维护的东西。
3. **单实例不许在这里判断**：`worker.lock` + `EXIT_ALREADY_RUNNING=3` 是唯一事实源；
   包装里再判一次就是同一判断的第二份实现（必然漂移）。
4. **默认不安装**：注册计划任务会在**用户机器上**留下 SYSTEM 身份的持久对象 ——
   脚本默认动作必须是只读的 `--check`。
5. **任务名只有一个字面量**：注册/查询/删除三处各写一份就会漂移
   （本项目实测过"同一 key 写在 3 处、只改一处 ⇒ 情报 5 个端点 403"）。

## 自证

`test_selfproof_removing_env_from_the_wrapper_would_fail` 把包装里的
`MOSS_ENV` 那一行去掉（在内存里改文本、不写盘），断言第 1 条判据**会红** ——
证明这几条不是"恒绿的存在性断言"。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "setup_worker_autostart_task.py"
WRAPPER = ROOT / "scripts" / "worker_autostart.ps1"

pytestmark = pytest.mark.skipif(
    not (SCRIPT.is_file() and WRAPPER.is_file()),
    reason="自启脚本未随仓库发布（.gitignore:88 /scripts/*）—— 显式降级，不静默跳过")


def _script_src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _wrapper_src() -> str:
    """包装脚本的**原始文本**（含注释，用于"必须存在 XXX"这类正向断言）。"""
    return WRAPPER.read_text(encoding="utf-8")


def _wrapper_code() -> str:
    """剥掉注释后的包装脚本文本（用于**禁止**类断言）。

    ⚠️ 为什么必须剥（本项目踩过同一形状）：包装的注释里逐字写着
    「单实例不靠这里：worker 自己的 OS 级锁（`worker.lock` …）」——
    拿原始文本做"不许出现 worker.lock"的判据，会被**它自己的说明**命中而假红
    （`CHG-0147` 的 AST 判据注释里记着同一条教训：文本判据会被自己的说明命中）。

    实现：先去掉 `<# … #>` 块注释，再逐行砍掉首个 ` #` 之后的内容并丢掉整行注释。
    局限（如实登记）：不处理字符串字面量里的 `#`；本脚本里没有这种写法。
    """
    text = re.sub(r"<#.*?#>", "", _wrapper_src(), flags=re.S)
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        cut = line.find(" #")
        out.append(line[:cut] if cut > 0 else line)
    return "\n".join(out)


def test_wrapper_sets_env_explicitly() -> None:
    """★ 约束 ①：包装里必须显式设 `MOSS_ENV`（且是 pilot）。"""
    src = _wrapper_src()
    assert re.search(r"\$env:MOSS_ENV\s*=", src), (
        "包装里没有显式设 MOSS_ENV ⇒ 计划任务起的进程会退化成 dev 库，"
        "用 dev 的库跑 pilot 的作业")
    assert "'pilot'" in src, "包装里的环境不是 pilot（与 api 实例不一致）"


def test_script_refuses_to_register_without_env_in_wrapper() -> None:
    """★ 约束 ① 的**运行时**护栏：包装缺 MOSS_ENV 时脚本拒绝注册。"""
    src = _script_src()
    assert "MOSS_ENV" in src and "拒绝注册" in src, (
        "脚本没有在注册前检查包装里的 MOSS_ENV —— 契约只写在文档里等于没写")


def test_default_action_is_read_only() -> None:
    """★ 约束 ④：默认（不带参数）必须只查不装。"""
    tree = ast.parse(_script_src())
    # 找出 main() 里对 --install 的处理，断言它只在显式 args.install 时才注册
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    main_src = ast.get_source_segment(_script_src(), funcs["main"]) or ""
    assert "args.install" in main_src, "main() 里没有按 --install 分支"
    # `_register_script()` 只允许在 args.install 分支里被调用
    calls = [i.lineno for i in ast.walk(funcs["main"])
             if isinstance(i, ast.Call) and isinstance(i.func, ast.Name)
             and i.func.id == "_register_script"]
    assert calls, "main() 里没有调用 _register_script（那 --install 做什么？）"
    install_guard = [i.lineno for i in ast.walk(funcs["main"])
                     if isinstance(i, ast.If)
                     and "args.install" in (ast.get_source_segment(
                         _script_src(), i.test) or "")]
    assert install_guard, "找不到 args.install 的分支"
    assert calls[0] > install_guard[0], (
        "注册动作发生在 args.install 分支**之前** ⇒ 默认动作会真的装到用户机器上")


def test_action_matches_existing_task_shape() -> None:
    """★ 约束 ②：动作写法与既有任务同形（不新造）。"""
    src = _script_src()
    for piece in ("powershell.exe", "-NoProfile", "-NonInteractive",
                  "-WindowStyle Hidden", "-ExecutionPolicy Bypass", "-File"):
        assert piece in src, f"动作里缺少既有写法片段 {piece!r}"


def test_single_instance_is_not_reimplemented_in_the_wrapper() -> None:
    """★ 约束 ③：包装里**不许**自己判断"是否已在跑"（那是锁的职责）。

    ⚠️ 断言跑在**剥过注释**的文本上（`_wrapper_code()`）：注释里逐字解释
    "单实例靠 worker.lock" 是应该的，把它当违规就是判据被自己的说明命中。
    """
    src = _wrapper_code()
    for forbidden in (r"Get-Process", r"worker\.lock", r"Test-Path\s+\$?\w*lock",
                      r"already\s+running"):
        assert not re.search(forbidden, src), (
            f"包装里出现了 {forbidden!r} —— 单实例判断只允许有一处（worker 的 OS 级锁），"
            "第二份实现必然与锁漂移")
    assert "start-worker" in src, "包装没有调用 start-worker（那它做什么？）"


def test_task_name_is_a_single_literal() -> None:
    """★ 约束 ⑤：任务名只有一个字面量（注册/查询/删除都读常量）。"""
    src = _script_src()
    literals = re.findall(r'["\'](Moss[A-Za-z]+)["\']', src)
    assert literals, "找不到任务名字面量"
    assert set(literals) == {"MossWorkerAutostart"}, (
        f"脚本里出现了多个任务名字面量：{sorted(set(literals))} —— "
        "注册与删除读不同的名字，就会删不掉自己装的东西")
    assert re.search(r"TASK_NAME\s*=\s*[\"']MossWorkerAutostart[\"']", src), (
        "任务名不是唯一的常量定义")


def test_selfproof_removing_env_from_the_wrapper_would_fail() -> None:
    """★ 自证：把包装里的 `MOSS_ENV` 去掉（**内存里改，不写盘**）⇒ 第 1 条必红。"""
    src = _wrapper_src()
    broken = re.sub(r"\$env:MOSS_ENV\s*=\s*'[^']*'", "", src)
    assert broken != src, "这条自证的前置条件不成立：包装里本来就没有 MOSS_ENV 赋值"
    assert not re.search(r"\$env:MOSS_ENV\s*=", broken), "变异没生效"
    # 断言"用变异后的文本去跑第 1 条判据会失败"——判据本体是纯文本断言，可直接复用
    with pytest.raises(AssertionError):
        assert re.search(r"\$env:MOSS_ENV\s*=", broken), (
            "包装里没有显式设 MOSS_ENV ⇒ 计划任务起的进程会退化成 dev 库")
