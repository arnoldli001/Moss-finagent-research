"""证券名称解析器测试：代码提取、精确/最长匹配、名称表不可用降级。"""

import json
import sys

import pandas as pd
import pytest

from src.infrastructure.connectors import security_resolver as sr


@pytest.fixture(autouse=True)
def _reset_memory_cache():
    sr._pairs_cache = None
    yield
    sr._pairs_cache = None


@pytest.fixture
def fake_pairs(monkeypatch):
    pairs = (
        ("300308", "中际旭创"),
        ("600000", "浦发银行"),
        ("000063", "中兴通讯"),
    )
    monkeypatch.setattr(sr, "_pairs_cache", None)
    monkeypatch.setattr(sr, "_name_pairs", lambda: pairs)
    return pairs


def test_extract_code():
    assert sr.extract_code("中际旭创300308怎么样") == "300308"
    assert sr.extract_code("目标 600519") == "600519"
    # 前后紧邻字母/数字时不切分（手机号、英文token）
    assert sr.extract_code("abc600519") is None
    assert sr.extract_code("13800000000") is None
    assert sr.extract_code("") is None


def test_resolve_code_lookup(fake_pairs):
    assert sr.resolve_stock_sync("600000") == ("600000", "浦发银行")


def test_resolve_code_when_table_unavailable(monkeypatch):
    # 名称表不可用时，6位代码本身仍有效（name为空串）
    def boom():
        raise ConnectionError("offline")

    monkeypatch.setattr(sr, "_name_pairs", boom)
    assert sr.resolve_stock_sync("300308") == ("300308", "")


def test_resolve_exact_name(fake_pairs):
    assert sr.resolve_stock_sync("中际旭创") == ("300308", "中际旭创")


def test_resolve_longest_name_in_question(monkeypatch):
    # 短名与长名同时出现在问句中时取最长匹配，避免"中际"截胡"中际旭创"
    monkeypatch.setattr(sr, "_name_pairs", lambda: (
        ("000001", "中际股份"), ("300308", "中际旭创")))
    assert sr.resolve_stock_sync("请问中际旭创目前估值如何") == ("300308", "中际旭创")


def test_resolve_name_table_unavailable(monkeypatch):
    def boom():
        raise ConnectionError("offline")

    monkeypatch.setattr(sr, "_name_pairs", boom)
    assert sr.resolve_stock_sync("中际旭创") is None  # 降级None，不阻断主链路
    assert sr.resolve_stock_sync("") is None


async def test_resolve_stock_async(fake_pairs):
    assert await sr.resolve_stock("中际旭创") == ("300308", "中际旭创")


class _FakeAkNames:
    def stock_info_a_code_name(self):
        return pd.DataFrame({"code": ["300308"], "name": ["中际旭创"]})


def test_fetch_persists_disk_cache(monkeypatch, tmp_path):
    cache = tmp_path / "security_names.json"
    monkeypatch.setattr(sr, "_CACHE_FILE", cache)
    monkeypatch.setitem(sys.modules, "akshare", _FakeAkNames())
    pairs = sr._fetch_and_persist()
    assert pairs == (("300308", "中际旭创"),)
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["pairs"] == [["300308", "中际旭创"]]
    # 新鲜磁盘缓存直接命中，不再发起网络（executor被换成会爆炸的）
    loaded, fresh = sr._load_disk()
    assert fresh and loaded == pairs

    class BoomExecutor:
        def submit(self, *a, **k):
            raise AssertionError("新鲜缓存不应触发网络拉取")

    monkeypatch.setattr(sr, "_executor", BoomExecutor())
    assert sr._name_pairs() == pairs


def test_load_disk_missing_or_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_CACHE_FILE", tmp_path / "nope.json")
    assert sr._load_disk() == (None, False)
    bad = tmp_path / "security_names.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(sr, "_CACHE_FILE", bad)
    assert sr._load_disk() == (None, False)
