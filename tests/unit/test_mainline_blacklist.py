"""板块黑名单（`configs/sector_blacklist.yaml`）单元测试。

## 这里测的核心是**失败语义**，不是文件内容

黑名单最危险的失败模式不是"读不到"，而是**读不到时被当成空集合**：
空集合的含义是"不排除任何板块"，于是板块池从 445 静默涨回 933，
所有排名、告警数量、回测结论随之改变，**而且不会有任何报错**。

因此 `load_sector_blacklist` 在不可用时返回 `None` 而不是 `frozenset()`，
调用方据此回退到旧的 `visible = 1` 行为。这个区分必须有测试守着 ——
后来的人很容易把"返回 None 太麻烦"改成"返回空集合"。

第二个重点是**清单是冻结的**：新板块不在清单里就该入池。
这是用户明确要求的规则（"未来有新增概念板块需要同步，不作为黑名单"），
一旦有人把选池逻辑改回 `WHERE visible = 1`，新概念就会被静默漏掉。

测试用 monkeypatch 替换 `load_yaml_config` 而不是写临时文件：
`load_yaml_config` 固定解析 `PROJECT_ROOT/configs`（不看 CWD），
写临时文件测不到这段逻辑，只会测到 YAML 库本身。
"""

from __future__ import annotations

import pytest

from src.mainline.config import (
    load_sector_blacklist,
    reset_blacklist_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个用例前后都清缓存（缓存是模块级的，否则用例互相污染）。"""
    reset_blacklist_cache()
    yield
    reset_blacklist_cache()


def _patch(monkeypatch, payload):
    """把 `load_yaml_config` 换成直接返回 `payload` 的假实现。"""
    monkeypatch.setattr("src.mainline.config.load_yaml_config",
                        lambda name, **kwargs: payload)


def test_reads_codes_from_dict_entries(monkeypatch):
    _patch(monkeypatch, {"codes": [
        {"code": "865005.TI", "name": "风电"},
        {"code": "865004.TI", "name": "苹果概念股"},
        {"code": "875001.TI", "name": "某隐藏概念"}]})
    assert load_sector_blacklist("x.yaml") == frozenset(
        {"865005.TI", "865004.TI", "875001.TI"})


def test_missing_file_returns_none_not_empty_set(monkeypatch):
    """**最关键的一条**：读不到必须返回 None。

    返回 `frozenset()` 会让调用方以为"没有黑名单"，
    板块池静默从 445 涨回 933 且不报错。
    """
    _patch(monkeypatch, None)
    assert load_sector_blacklist("missing.yaml") is None


def test_empty_codes_returns_none(monkeypatch):
    """清单在但一个代码都没有 —— 同样按不可用处理。

    空清单语义上等价于"不排除任何板块"，与"文件丢了"一样危险。
    """
    _patch(monkeypatch, {"codes": []})
    assert load_sector_blacklist("x.yaml") is None


def test_non_dict_payload_returns_none(monkeypatch):
    """YAML 顶层不是映射（比如写成了一个列表）时按不可用处理。"""
    _patch(monkeypatch, ["865005.TI"])
    assert load_sector_blacklist("x.yaml") is None


def test_plain_string_entries_are_accepted(monkeypatch):
    """同时支持 `- "865005.TI"` 与 `- {code: ...}` 两种写法。"""
    _patch(monkeypatch, {"codes": ["865005.TI", {"code": "865004.TI"}]})
    assert load_sector_blacklist("x.yaml") == frozenset(
        {"865005.TI", "865004.TI"})


def test_blank_and_malformed_entries_are_skipped(monkeypatch):
    """空串/纯空白条目不能进集合 —— 它们会匹配不到任何板块但占位。"""
    _patch(monkeypatch, {"codes": ["865005.TI", "", "   ", {}, {"code": ""}]})
    assert load_sector_blacklist("x.yaml") == frozenset({"865005.TI"})


def test_loader_exception_is_treated_as_unavailable(monkeypatch):
    """读盘抛异常时同样返回 None，不能把异常抛给调用方（面板要能开）。"""
    def boom(name, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr("src.mainline.config.load_yaml_config", boom)
    assert load_sector_blacklist("x.yaml") is None


def test_result_is_cached(monkeypatch):
    payload = {"codes": [{"code": "865005.TI"}]}
    _patch(monkeypatch, payload)
    first = load_sector_blacklist("x.yaml")
    payload["codes"] = []                      # 改了源数据
    assert load_sector_blacklist("x.yaml") is first


def test_reset_cache_picks_up_changes(monkeypatch):
    payload = {"codes": [{"code": "865005.TI"}]}
    _patch(monkeypatch, payload)
    assert load_sector_blacklist("x.yaml") == frozenset({"865005.TI"})
    payload["codes"] = [{"code": "865004.TI"}]
    reset_blacklist_cache()
    assert load_sector_blacklist("x.yaml") == frozenset({"865004.TI"})


def test_universe_config_exposes_blacklist_file():
    from src.mainline.config import UniverseConfig

    assert UniverseConfig().blacklist_file == "sector_blacklist.yaml"


# ==================================================================
# 与真清单 / 真库的集成（无数据时 skip，不让环境问题变成测试失败）
# ==================================================================


def test_shipped_blacklist_is_loadable_and_substantial():
    """仓库里自带的那份清单必须能读出来，且规模合理。

    清单被误清空或格式改坏时，运行时的表现只是"池子变大了"，
    极难被发现 —— 所以这里把规模钉住。

    ⚠️ 这里用**下限**而不是精确条数：清单本身会随板块上下架而变
    （实测从 488 变成 420，`865` 段 250 个的覆盖没变），
    钉死精确值只会让每次合理的清单更新都变成测试失败。
    真正的结构性覆盖由 `test_shipped_blacklist_covers_retired_865_family` 保证。
    """
    codes = load_sector_blacklist("sector_blacklist.yaml")
    assert codes is not None, "configs/sector_blacklist.yaml 读不出来"
    assert len(codes) >= 300, f"清单疑似被误清空：只有 {len(codes)} 条"


def test_shipped_blacklist_covers_retired_865_family():
    """`865xxx` 是整套已淘汰的概念体系（250 个），必须全部在清单里。

    它们与 `875xxx`/`885xxx` 存在同名不同码的换代关系，
    漏进来会让同名板块在池里出现两次。
    """
    codes = load_sector_blacklist("sector_blacklist.yaml") or frozenset()
    family = {code for code in codes if code.startswith("865")}
    assert len(family) == 250, f"865 段数量变了：{len(family)}"


def test_blacklist_boards_are_absent_from_pool():
    """黑名单板块不应出现在主线板块池里（真库集成检查）。"""
    from src.mainline.config import load_config
    from src.mainline.datastore import MainlineDataStore

    codes = load_sector_blacklist("sector_blacklist.yaml") or frozenset()
    store = MainlineDataStore(config=load_config())
    try:
        boards = {item.code for item in store.boards()}
    except Exception as exc:  # noqa: BLE001 无库/无表时跳过
        pytest.skip(f"板块池不可读：{type(exc).__name__}")
    if not boards:
        pytest.skip("板块池为空（尚未导入）")
    leaked = sorted(boards & set(codes))
    assert not leaked, f"黑名单板块泄漏进池：{leaked[:5]}"
    assert not any(code.startswith("865") for code in boards)


def test_pool_import_reports_selection_mode():
    """同步 message 要写明走黑名单还是回退 —— 回退会让入池数量突变。"""
    import sqlite3

    from src.mainline.config import load_config
    from src.mainline.datastore import MainlineDataStore

    store = MainlineDataStore(config=load_config())
    try:
        result = store.import_crowding_pool()
    except (sqlite3.Error, OSError) as exc:
        pytest.skip(f"拥挤度库不可读：{type(exc).__name__}")
    if result.status == "skipped":
        pytest.skip("拥挤度库不存在")
    assert "黑名单" in result.message or "回退" in result.message
