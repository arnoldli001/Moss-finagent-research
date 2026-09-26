"""`manage.py` 的启动自检单测。

背景（2026-09-23 用户报障"量化选股的模型又丢了"）：根因不是模型文件丢了，
而是**服务被用系统 Python 启起来了**（`C:\\veighna_studio`，没有 lightgbm）。
`moss_selector` 的模型是 LightGBM 模型，`joblib.load` 反序列化时要
`import lightgbm` —— 于是 6 个模型全部加载失败被跳过，`load_model_set` 抛
「没有可用的模型文件」，界面就报"模型丢了"并反复自动重训。

所以在 `manage.py start` 的启动自检里加了依赖探测：缺必备依赖**拒绝启动**，
而不是让它伪装成"文件丢失"。本文件锁住这个行为。
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

import manage  # noqa: E402


def test_required_dependencies_include_lightgbm_and_sklearn():
    """必备清单必须含 lightgbm（这就是那次故障的根因）。"""
    assert "lightgbm" in manage._RUNTIME_REQUIRED
    assert "sklearn" in manage._RUNTIME_REQUIRED


def test_missing_runtime_dependencies_is_empty_when_all_present(monkeypatch):
    monkeypatch.setattr(manage, "_RUNTIME_REQUIRED", ())
    assert manage.missing_runtime_dependencies() == []


def test_missing_runtime_dependencies_reports_unimportable(monkeypatch):
    """导入失败（ModuleNotFoundError / ImportError 都算）→ 报出来。"""
    monkeypatch.setattr(manage, "_RUNTIME_REQUIRED", ("lightgbm", "sklearn"))

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name in ("lightgbm", "sklearn"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert manage.missing_runtime_dependencies() == ["lightgbm", "sklearn"]


def test_missing_runtime_dependencies_reports_only_the_missing_one(monkeypatch):
    monkeypatch.setattr(manage, "_RUNTIME_REQUIRED", ("lightgbm", "sklearn"))

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "lightgbm":
            raise ModuleNotFoundError("No module named 'lightgbm'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert manage.missing_runtime_dependencies() == ["lightgbm"]


@pytest.mark.parametrize("exc", [ImportError("boom"), OSError("dll load failed")])
def test_missing_runtime_dependencies_swallows_non_module_errors(monkeypatch, exc):
    """不只看 ModuleNotFoundError：DLL 加载失败（Windows 上 lightgbm 常见）
    同样是"这个解释器不能用它"，必须一并报出来。"""
    monkeypatch.setattr(manage, "_RUNTIME_REQUIRED", ("lightgbm",))

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "lightgbm":
            raise exc
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert manage.missing_runtime_dependencies() == ["lightgbm"]


def test_prepare_environment_refuses_when_dependency_missing(monkeypatch):
    """依赖缺失时 `_prepare_environment` 必须返回非 0 退出码（拒绝启动）。"""
    monkeypatch.setattr(manage, "missing_runtime_dependencies", lambda: ["lightgbm"])
    extra, code = manage._prepare_environment(None)
    assert code == 1
    assert extra == {} or isinstance(extra, dict)      # 不注入环境变量也允许
