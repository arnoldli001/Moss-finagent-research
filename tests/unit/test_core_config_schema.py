"""core.config 与 core.schemas 基础测试。"""

import ast
import os
import subprocess
import sys

from src.core.config import Settings, get_settings
from src.core.schemas import Confidence, TraceStep


def test_settings_defaults():
    settings = get_settings()
    assert settings.app_name == "Moss-FinAgent-Research"
    assert settings.api_port == 8100
    assert settings.ollama_base_url == "http://localhost:11434"


def test_trace_step_types():
    step = TraceStep(step=1, step_type="data_retrieval", description="获取PPI数据")
    assert step.duration_ms is None
    assert Confidence.HIGH.value == "high"


# ==================== QMT 开关（data_source.qmt_enabled） ====================
#
# 背景：本机 QMT 终端已失去行情权限且不在运行（127.0.0.1:58610 不通），
# 所以它默认**关闭**、且开启后也只排在日线链最后。下面几条把"默认关 / 真值可开 /
# 日线链顺序"钉死 —— 顺序错了的表现是"面板停在三周前"，很难从现象倒推回原因。


def _settings_with_env(value: str | None) -> Settings:
    """在子进程里按给定 QMT_ENABLED 求值（Settings 在类定义时就读了 os.environ）。"""
    code = (
        "from src.core.config import Settings;"
        "print(Settings().qmt_enabled)"
    )
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if value is None:
        env.pop("QMT_ENABLED", None)
    else:
        env["QMT_ENABLED"] = value
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=env, check=True)
    return result.stdout.strip() == "True"


def test_qmt_disabled_by_default():
    """不给 QMT_ENABLED 时必须默认关闭（没有权限的部署不该为它付一次必然失败的等待）。"""
    assert _settings_with_env(None) is False
    assert _settings_with_env("") is False
    assert _settings_with_env("0") is False
    assert _settings_with_env("false") is False


def test_qmt_enabled_truthy_parse():
    """1/true/yes/on（大小写不敏感）都算打开。"""
    for value in ("1", "true", "TRUE", "True", "yes", "on", "ON"):
        assert _settings_with_env(value) is True, value


def _route_names(tree: ast.AST) -> list[str]:
    """从 AST 里按出现顺序取出 `routes.append((<指标名>, ...))` 的第一个名字。

    实参形态是 `(akshare, AkshareConnector.supports)`（Name，不是 Call），
    QMT 分支里是 `(qmt, XtQuantConnector.supports)` —— 统一按首元素取 id，
    并在开关分支里加 `if:` 前缀，这样"顺序"与"是否受开关控制"一次都能看出来。
    """
    names: list[str] = []

    def first_name(arg: ast.expr) -> str | None:
        if isinstance(arg, ast.Tuple) and arg.elts:
            head = arg.elts[0]
            if isinstance(head, ast.Name):
                return head.id
            if isinstance(head, ast.Call) and isinstance(head.func, ast.Name):
                return head.func.id
        return None

    def walk(node: ast.AST, guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and isinstance(child.test, ast.Attribute) \
                    and child.test.attr == "qmt_enabled":
                for sub in ast.walk(child):
                    if (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "append"
                            and isinstance(sub.func.value, ast.Name)
                            and sub.func.value.id == "routes" and sub.args):
                        name = first_name(sub.args[0])
                        if name:
                            names.append(f"if:{name}")
                continue
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "append"
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "routes" and child.args):
                name = first_name(child.args[0])
                if name:
                    names.append(("if:" if guarded else "") + name)
            walk(child, guarded)

    walk(tree, False)
    return names


def test_daily_chain_order_in_runtime_source():
    """日线链顺序：在线源在前、停更的本地CSV与QMT在后，且 QMT 受开关控制。

    直接解析 `src/api/runtime.py` 的 AST：`build_runtime()` 会连带装配 LLM 网关、
    图、多个子系统，单测里跑它既慢又脆；而这里要钉住的只是**顺序与开关**。
    """
    import src.api.runtime as runtime_module

    with open(runtime_module.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    order = _route_names(tree)

    # 只挑出**日线行情链**这一段的相对顺序（其后还有申万估值/渗透率/模拟产业等非行情路由，
    # 它们与日线链无关，不该被卷进这条断言）
    quote_chain = [name for name in order if name in {
        "AkshareConnector", "TencentDailyConnector", "TushareConnector",
        "BaostockConnector", "LocalCsvConnector", "if:XtQuantConnector"}]
    assert quote_chain == [
        "AkshareConnector", "TencentDailyConnector", "TushareConnector",
        "BaostockConnector", "LocalCsvConnector", "if:XtQuantConnector",
    ], f"日线链顺序不对: {quote_chain}"
    # QMT 必须受开关控制：未开启时不得注册（否则每次取数先吃一次必然失败的连接）
    assert "XtQuantConnector" not in order, "不得注册无条件的 QMT 路由"
    assert "if:XtQuantConnector" in order


def test_baostock_connector_registered_after_online_sources():
    """baostock 是免 token 的第四道兜底，必须真的进了链（不是只写了类）。"""
    from src.infrastructure.connectors.baostock_connector import BaostockConnector

    assert BaostockConnector.supports("stock_close:600036")
    assert BaostockConnector.supports("index_close:000300")
    assert BaostockConnector.supports("etf_close:510300")
