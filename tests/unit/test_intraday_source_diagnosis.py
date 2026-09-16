"""新浪兜底源的失败诊断：必须区分"被限流封禁"与"确实没有交易日"。

实测背景（2026-09-16）：新浪对本机 IP 返回 **HTTP 456 + HTML「拒绝访问」页**
（页面自称"IP 存在异常访问已被封禁，5~60 分钟自动解封"）。akshare 拿 HTML 去解析
JSON，抛的是 `Expecting value: line 1 column 1 (char 0)`；
而原实现把这类错误统一报成「新浪逐笔无可取交易日」—— 排查方向会被直接带偏到
交易日历上，而真实原因是限流，两者的处置完全不同（等解封 vs 查日历/接口）。
"""
from __future__ import annotations

import sys
import types

import pytest

from src.core.exceptions import DataFetchError
from src.intraday.sources import SinaSource


def _fake_akshare(error: Exception) -> types.ModuleType:
    module = types.ModuleType("akshare")

    def boom(*_args, **_kwargs):
        raise error

    module.stock_intraday_sina = boom  # type: ignore[attr-defined]
    return module


def test_non_json_response_reported_as_rate_limit(monkeypatch) -> None:
    """HTML 拒绝页 → 报"返回非 JSON / 限流封禁"，而不是"无可取交易日"。"""
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(
        ValueError("Expecting value: line 1 column 1 (char 0)")))
    with pytest.raises(DataFetchError) as excinfo:
        SinaSource._load_ticks("600519")  # noqa: SLF001 测的就是这个诊断分支
    message = str(excinfo.value)
    assert "非 JSON" in message, f"应点明拿到的是非 JSON：{message}"
    assert "限流" in message or "封禁" in message, \
        "应给出「被限流/封禁」这个真实原因"
    assert "无可取交易日" not in message, "不能再用误导性的旧文案"


def test_explicit_access_denied_marker_is_recognized(monkeypatch) -> None:
    """即使错误文本里直接带「拒绝访问」也应被识别（不依赖 JSON 解析异常类型）。"""
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(
        RuntimeError("HTTP 456 拒绝访问")))
    with pytest.raises(DataFetchError) as excinfo:
        SinaSource._load_ticks("600519")  # noqa: SLF001
    assert "非 JSON" in str(excinfo.value)


def test_plain_network_error_keeps_missing_day_wording(monkeypatch) -> None:
    """普通网络错误仍报"无可取交易日"——不能把所有失败都说成被封禁。"""
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(
        TimeoutError("connection timed out")))
    with pytest.raises(DataFetchError) as excinfo:
        SinaSource._load_ticks("600519")  # noqa: SLF001
    message = str(excinfo.value)
    assert "无可取交易日" in message
    assert "非 JSON" not in message


def test_missing_akshare_gives_install_hint(monkeypatch) -> None:
    """akshare 没装时要给出安装命令，而不是笼统失败。"""
    monkeypatch.setitem(sys.modules, "akshare", None)
    with pytest.raises(DataFetchError) as excinfo:
        SinaSource._load_ticks("600519")  # noqa: SLF001
    assert "akshare" in str(excinfo.value)
