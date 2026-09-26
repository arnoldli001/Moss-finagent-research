"""套餐配置测试：资源上限 / 功能权限 / 定价（管理员可配的那份）。

对应设计：§4.6、§8.6.10。

## 本文件要钉死的三件事

1. **默认值不能悄悄变**：试用档的额度、VIP 的月费这些是**商业口径**，
   改错了就是"试用用户拿到 VIP 额度"这类静默放宽；
2. **非法输入必须被拒**（未知功能键/负数/NaN）——不拒的话会写进配置，
   而配置是全局生效的：写坏一个字段，该等级所有人立刻受影响；
3. **写盘是原子的**：写一半被杀不能让所有等级回退出厂默认
   （那等于"所有人突然被降级"）。

## 为什么用临时文件而不是真配置

测"改套餐"不能动 `configs/platform_tiers.json` —— 那是运行中的配置，
测试污染它会让**正在使用的服务**按测试的临时额度跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.domain.platform.config import (
    FEATURES,
    RESOURCE_FIELDS,
    TIER_ORDER,
    PlatformConfigStore,
    TierConfigError,
    plan_to_json,
    reset_platform_config,
)


@pytest.fixture()
def store(tmp_path, monkeypatch) -> PlatformConfigStore:
    """隔离的配置文件（并把单例指向它，避免读到仓库里那份）。"""
    path = tmp_path / "tiers.json"
    monkeypatch.setenv("MOSS_TIER_CONFIG", str(path))
    reset_platform_config()
    yield PlatformConfigStore(str(path))
    reset_platform_config()


# ======================================================================
# 一、出厂默认（商业口径）
# ======================================================================

def test_defaults_cover_all_tiers_and_features(store) -> None:
    plans = store.plans()
    assert set(plans) == set(TIER_ORDER), f"实际 {sorted(plans)}"
    for key, plan in plans.items():
        assert set(plan.features) == set(FEATURES), f"{key} 缺功能键"
        assert set(plan.pricing) == set(FEATURES), f"{key} 缺定价键"
        assert set(plan.resources) == set(RESOURCE_FIELDS), f"{key} 缺资源键"


def test_trial_is_strictly_weaker_than_vip(store) -> None:
    """★ 试用档的每项资源都不得超过 VIP —— 否则"试用"名不副实。

    这类错误最危险：配错一次，所有试用用户拿到 VIP 额度，
    而界面上一切正常（没有报错、没有告警）。
    """
    trial = store.plans()["trial"].resources
    vip = store.plans()["vip"].resources
    for key in RESOURCE_FIELDS:
        assert trial[key] <= vip[key], f"{key}: 试用 {trial[key]} > VIP {vip[key]}"


def test_vip_is_not_stronger_than_admin(store) -> None:
    vip = store.plans()["vip"].resources
    admin = store.plans()["admin"].resources
    for key in RESOURCE_FIELDS:
        assert vip[key] <= admin[key], f"{key}: VIP {vip[key]} > 管理员 {admin[key]}"


def test_trial_does_not_get_expensive_features(store) -> None:
    """★ 贵的功能默认不给试用（竞价选股/量化选股/回测）。

    这几个是**卖点**：白送等于把付费理由拿掉了。
    """
    trial = store.plans()["trial"].features
    assert trial["quant.auction"] is False
    assert trial["quant.select"] is False
    assert trial["backtest"] is False
    assert trial["quant.intraday"] is True, "做T是核心体验，试用必须能开"


def test_admin_has_every_feature(store) -> None:
    admin = store.plans()["admin"].features
    assert all(admin[k] for k in FEATURES), "管理员档有功能被关掉了"


def test_admin_is_not_sellable(store) -> None:
    assert store.plans()["admin"].sellable is False
    assert store.plans()["vip"].sellable is True


# ======================================================================
# 二、读取与回退
# ======================================================================

def test_missing_file_falls_back_to_defaults(store, tmp_path) -> None:
    """★ 配置缺失 → 回退出厂默认（**受限可用**），而不是让平台起不来。

    500 是"谁都别用"，回退是"按默认套餐用" —— 后者才是可运维的。
    """
    assert not Path(store.path).exists()
    plans = store.plans()
    assert plans["vip"].resources["api_calls_per_minute"] > 0


def test_corrupt_file_falls_back_to_defaults(tmp_path, monkeypatch) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    monkeypatch.setenv("MOSS_TIER_CONFIG", str(path))
    reset_platform_config()
    plans = PlatformConfigStore(str(path)).plans()
    assert set(plans) == set(TIER_ORDER), "损坏配置没有回退"
    reset_platform_config()


def test_unknown_tier_falls_back_to_smallest(store) -> None:
    """★ fail-closed：未知等级按**最小**额度，而不是"不限制"。"""
    plan = store.plan("superuser")
    assert plan.key == "trial"
    assert store.feature_enabled("superuser", "backtest") is False
    assert store.feature_enabled("", "quant.auction") is False


def test_partial_file_is_merged_with_defaults(tmp_path, monkeypatch) -> None:
    """文件只写了部分字段时，其余走默认 —— 运维手改配置不会漏项。"""
    path = tmp_path / "partial.json"
    path.write_text(json.dumps({
        "plans": {"vip": {"label": "VIP 尊享",
                          "resources": {"api_calls_per_minute": 999}}}
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("MOSS_TIER_CONFIG", str(path))
    reset_platform_config()
    plan = PlatformConfigStore(str(path)).plan("vip")
    assert plan.label == "VIP 尊享"
    assert plan.resources["api_calls_per_minute"] == 999
    assert plan.resources["watchlist_stocks"] > 0, "未写的字段应保留默认"
    assert set(plan.features) == set(FEATURES)
    reset_platform_config()


# ======================================================================
# 三、写入校验（拒绝非法输入）
# ======================================================================

def test_rejects_unknown_tier(store) -> None:
    with pytest.raises(TierConfigError, match="未知等级"):
        store.save_plan("gold", {"label": "黄金"})


def test_rejects_unknown_feature_key(store) -> None:
    """★ 未知功能键必须拒。

    放行的话会写进配置、且**永远不生效**：管理员以为开了权限，
    用户却看不到页面 —— 这类"配了没用"最难排查。
    """
    with pytest.raises(TierConfigError, match="未知功能"):
        store.save_plan("vip", {"features": {"quant.options": True}})


def test_rejects_unknown_resource_key(store) -> None:
    with pytest.raises(TierConfigError, match="未知资源项"):
        store.save_plan("vip", {"resources": {"gpu_hours": 10}})


@pytest.mark.parametrize("bad", [-1, -100])
def test_rejects_negative_resources(store, bad) -> None:
    with pytest.raises(TierConfigError, match="不能为负"):
        store.save_plan("vip", {"resources": {"api_calls_per_minute": bad}})


@pytest.mark.parametrize("bad", ["abc", None, [1]])
def test_rejects_non_numeric_resources(store, bad) -> None:
    with pytest.raises(TierConfigError):
        store.save_plan("vip", {"resources": {"api_calls_per_minute": bad}})


def test_rejects_non_finite_price(store) -> None:
    """★ NaN/Infinity 必须在入口挡住。

    它们会**静默污染**所有价格计算（总价变 NaN，账单显示不出来），
    而 JSON 里的 NaN 不是合法 JSON、Python 却默认接受。
    """
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(TierConfigError, match="有限数字|不能为负"):
            store.save_plan("vip", {"pricing": {"backtest": bad}})


def test_dropped_monthly_price_is_ignored_not_fatal(store) -> None:
    """★ 已下线的 `monthly_price`：**忽略并留日志**，不能让保存失败。

    为什么这条测试重要：浏览器里可能还缓存着旧版前端包，它会继续发
    `monthly_price`。如果服务端改成报错，症状就是"资源管控页保存一律失败"，
    用户会以为整个面板坏了 —— 而真正要做的只是硬刷新页面。
    所以这里断言"保存照样成功，且文件里不再有这个键"。
    """
    plan = store.save_plan("vip", {"monthly_price": 1299.0,
                                   "resources": {"api_calls_per_minute": 222}})
    assert plan.resources["api_calls_per_minute"] == 222, "同一次请求的其它字段被丢了"
    assert not hasattr(plan, "monthly_price"), "字段应当已从模型中移除"
    raw = json.loads(Path(store.path).read_text(encoding="utf-8"))
    assert "monthly_price" not in raw["plans"]["vip"], (
        "月费又被打回配置文件了 —— 它已下线，不该再被持久化")


def test_rejects_empty_label(store) -> None:
    with pytest.raises(TierConfigError, match="不能为空"):
        store.save_plan("vip", {"label": "   "})


def test_failed_save_does_not_touch_file(store) -> None:
    """★ 校验失败时**不能**留下半个文件。

    套餐全局生效：写坏一个字段，该等级所有用户立刻受影响。
    """
    store.save_plan("vip", {"resources": {"api_calls_per_minute": 321}})
    before = Path(store.path).read_text(encoding="utf-8")
    with pytest.raises(TierConfigError):
        store.save_plan("vip", {"features": {"no.such.feature": True}})
    assert Path(store.path).read_text(encoding="utf-8") == before, (
        "校验失败却改了文件 —— 管理员会以为改动生效了")


# ======================================================================
# 四、写入生效
# ======================================================================

def test_save_then_reload_sees_the_change(store) -> None:
    store.save_plan("vip", {
        "resources": {"api_calls_per_minute": 888},
        "features": {"quant.auction": True},
        "pricing": {"quant.auction": 299.5},
    })
    fresh = PlatformConfigStore(str(store.path)).plan("vip")
    assert fresh.resources["api_calls_per_minute"] == 888
    assert fresh.features["quant.auction"] is True
    assert fresh.pricing["quant.auction"] == 299.5


def test_save_preserves_other_tiers(store) -> None:
    before = store.plan("trial").resources["watchlist_stocks"]
    store.save_plan("vip", {"resources": {"watchlist_stocks": 77}})
    assert store.plan("trial").resources["watchlist_stocks"] == before, (
        "改 VIP 影响到了试用档")
    assert store.plan("admin").resources["watchlist_stocks"] > 0


def test_write_leaves_no_temp_file(store) -> None:
    """原子写用临时文件，写完后不能留下它（否则下次读会困惑）。"""
    store.save_plan("vip", {"note": "临时文件检查"})
    leftovers = list(Path(store.path).parent.glob("*.tmp"))
    assert leftovers == [], f"残留临时文件：{leftovers}"


def test_saved_file_is_valid_json_and_ordered(store) -> None:
    """落盘必须是**合法 JSON 且等级有序**（权限从大到小）——便于人工评审。"""
    store.save_plan("trial", {"note": "顺序检查"})
    raw = json.loads(Path(store.path).read_text(encoding="utf-8"))
    assert list(raw["plans"]) == list(TIER_ORDER), (
        f"等级顺序不是 {TIER_ORDER}：{list(raw['plans'])}")


# ======================================================================
# 五、与自选池额度表的一致性（防两份口径打架）
# ======================================================================

def test_watchlist_resource_matches_pool_quota_table() -> None:
    """★ `platform_tiers.watchlist_stocks` 必须与 `TIER_QUOTAS` 不矛盾。

    这两个数字都表示"自选标的上限"：
      - `src/domain/quota/service.py` 的 `TIER_QUOTAS` 是**自选池模块**用的；
      - `configs/platform_tiers.json` 是**管理台展示与售卖**用的。
    两处不一致会出现"界面上说 100 只、实际加到 25 只就被拒"——
    用户会认为是 bug，而我们要花很久才能解释清是"两份配置"。
    """
    from src.domain.quota.service import TIER_QUOTAS

    store = PlatformConfigStore("configs/platform_tiers.json")
    plans = store.plans()
    for tier, quota in TIER_QUOTAS.items():
        if tier not in plans:
            continue
        p = plans[tier].resources
        assert p["watchlist_stocks"] == quota.watchlist_limit, (
            f"{tier}: 平台配置 {p['watchlist_stocks']} != 池模块 "
            f"{quota.watchlist_limit}")
        assert p["watchlist_pools"] == quota.pool_limit, (
            f"{tier}: 池数 {p['watchlist_pools']} != {quota.pool_limit}")


def test_plan_to_json_fills_missing_keys(store) -> None:
    """`plan_to_json` 必须补齐所有键。

    前端表格按 `FEATURES`/`RESOURCE_FIELDS` 全量渲染，缺键会显示成空白，
    管理员分不清"没配"与"配成了 0"。
    """
    store.save_plan("vip", {"features": {}})     # 不变
    data = plan_to_json(store.plan("vip"))
    assert set(data["features"]) == set(FEATURES)
    assert set(data["pricing"]) == set(FEATURES)
    assert set(data["resources"]) == set(RESOURCE_FIELDS)
