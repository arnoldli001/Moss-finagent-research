"""权重档案 API 集成测试（真实 Service + 真实 SQLite 仓储 + TestClient）。

为什么不只测仓储：这里要钉住的几条语义都发生在**路由层**，而且是会丢数据的那种：
  1. `PUT` 是**部分更新**：只提交日线权重不能把已有的分时权重清空；
  2. 提交全量权重时合计必须=100，否则 400（总分刻度依赖它）；
  3. 未知因子名/负权重必须 400，坏数据进不了库；
  4. 删除幂等；
  5. 档案一旦存在，`/factors?code=` 与 `/weight-profiles/{code}` 都要如实报出
     "用的是档案口径"（不能静默生效）。

刻意不打网络：档案读写路径不依赖任何行情源，`apply_character` 相关的用例单独跳过。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.intraday_weights import router
from src.infrastructure.repositories.intraday_profile_sqlite_repo import (
    IntradayProfileSqliteRepository,
)
from src.intraday.level_fit import LevelFitResult
from src.intraday.service import IntradayService

CODE = "300308"


def _weights(total: float = 100.0) -> dict[str, float]:
    """一份合计恰好等于 total 的分时权重（14 因子平摊后补齐余量）。"""
    keys = ("box", "vwap", "boll", "macd", "kdj_rsi", "sentiment", "news",
            "index_volume", "board_rank", "overseas", "chan", "chip",
            "cycle", "character")
    each = round(total / len(keys), 2)
    result = {key: each for key in keys}
    result["box"] = round(result["box"] + total - sum(result.values()), 2)
    return result


def _daily_weights() -> dict[str, float]:
    return {"trend": 26, "chan_daily": 18, "volume": 16, "position": 14,
            "signal_rule": 8, "cycle": 8, "character": 10}


@pytest.fixture
def client(tmp_dir: str):
    db_path = os.path.join(tmp_dir, "profiles_api.db")
    repo = IntradayProfileSqliteRepository(db_path)
    service = IntradayService(profile_repo=repo)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(intraday=service, intraday_profile_repo=repo)
    with TestClient(app) as test_client:
        yield test_client


# ==================== 因子目录 ====================

def test_factors_endpoint_returns_catalog_for_both_modes(client) -> None:
    intraday = client.get("/api/v1/intraday/factors", params={"mode": "intraday"})
    assert intraday.status_code == 200
    body = intraday.json()
    assert len(body["factors"]) == 14
    assert body["weights_sum"] == pytest.approx(100.0)
    weights = {item["key"]: item["default_weight"] for item in body["factors"]}
    # cycle/character 在两种模式里同名不同权重 —— 目录必须按模式分开取
    assert weights["cycle"] == pytest.approx(6.0)
    assert weights["character"] == pytest.approx(3.0)
    assert sum(item["from_skill"] for item in body["factors"]) == 4

    daily = client.get("/api/v1/intraday/factors", params={"mode": "daily"}).json()
    assert len(daily["factors"]) == 7
    daily_weights = {item["key"]: item["default_weight"] for item in daily["factors"]}
    assert daily_weights["cycle"] == pytest.approx(10.0)
    assert daily_weights["character"] == pytest.approx(10.0)
    assert daily_weights["trend"] == pytest.approx(20.0)
    assert len(daily["templates"]) == 5


def test_factors_endpoint_rejects_unknown_mode(client) -> None:
    assert client.get("/api/v1/intraday/factors",
                      params={"mode": "weekly"}).status_code == 422


# ==================== 档案 CRUD ====================

def test_profile_lifecycle_and_partial_update(client) -> None:
    # 1) 先只写日线权重
    first = client.put(f"/api/v1/intraday/weight-profiles/{CODE}",
                       json={"daily_weights": _daily_weights(),
                             "note": "只改日线"})
    assert first.status_code == 200, first.text
    saved = first.json()["profile"]
    assert saved["daily_weights"]["trend"] == pytest.approx(26.0)
    assert saved["weights"] == {}

    # 2) 再只写分时权重 —— 日线权重必须原样保留（部分更新的核心断言）
    second = client.put(f"/api/v1/intraday/weight-profiles/{CODE}",
                        json={"weights": _weights(), "thresholds": {"action": 32, "hint": 22}})
    assert second.status_code == 200, second.text
    saved = second.json()["profile"]
    assert saved["weights"]["box"] == pytest.approx(_weights()["box"])
    assert saved["daily_weights"] == pytest.approx(_daily_weights())
    assert saved["thresholds"] == {"action": 32.0, "hint": 22.0}
    assert saved["note"] == "只改日线", "未提交的字段应沿用原值"

    # 3) 列出来
    listing = client.get("/api/v1/intraday/weight-profiles").json()
    assert listing["count"] == 1
    assert listing["profiles"][0]["code"] == CODE
    assert "dim_intraday_profile" in listing["source"]

    # 4) 单只票：档案 + 当前生效口径
    detail = client.get(f"/api/v1/intraday/weight-profiles/{CODE}").json()
    assert detail["profile"]["code"] == CODE
    assert detail["effective"]["weights"]["box"] == pytest.approx(
        saved["weights"]["box"])
    assert "权重档案" in detail["effective"]["source"]
    assert detail["effective"]["thresholds"]["action"] == pytest.approx(32.0)

    # 5) 档案一旦存在，因子目录必须如实报出来（不能静默生效）
    catalog = client.get("/api/v1/intraday/factors",
                         params={"mode": "intraday", "code": CODE}).json()
    assert "权重档案" in catalog["override_source"]
    assert catalog["effective_weights"]["box"] == pytest.approx(
        saved["weights"]["box"])
    assert catalog["override"]["thresholds"]["action"] == pytest.approx(32.0)

    # 6) 删除幂等
    assert client.delete(
        f"/api/v1/intraday/weight-profiles/{CODE}").json()["removed"] is True
    assert client.delete(
        f"/api/v1/intraday/weight-profiles/{CODE}").json()["removed"] is False
    assert client.get("/api/v1/intraday/weight-profiles").json()["count"] == 0
    detail = client.get(f"/api/v1/intraday/weight-profiles/{CODE}").json()
    assert detail["profile"] is None
    assert "全局口径" in detail["effective"]["source"]


def test_missing_profile_is_none_not_error(client) -> None:
    detail = client.get("/api/v1/intraday/weight-profiles/600036")
    assert detail.status_code == 200
    assert detail.json()["profile"] is None


# ==================== 校验 ====================

def test_full_weights_must_sum_to_100(client) -> None:
    resp = client.put(f"/api/v1/intraday/weight-profiles/{CODE}",
                      json={"weights": _weights(total=120.0)})
    assert resp.status_code == 400
    assert "合计必须为100" in resp.json()["detail"]


def test_unknown_factor_name_is_rejected(client) -> None:
    resp = client.put(f"/api/v1/intraday/weight-profiles/{CODE}",
                      json={"weights": {"boll_band": 8.0}})
    assert resp.status_code == 400
    assert "未知因子" in resp.json()["detail"]


def test_negative_weight_is_rejected(client) -> None:
    resp = client.put(f"/api/v1/intraday/weight-profiles/{CODE}",
                      json={"weights": {"box": -5.0}})
    assert resp.status_code == 400
    assert "不得为负" in resp.json()["detail"]


def test_daily_weights_validated_against_daily_catalog(client) -> None:
    """日线权重不能塞分时因子名（两套目录不同，混用必须报错）。"""
    resp = client.put(f"/api/v1/intraday/weight-profiles/{CODE}",
                      json={"daily_weights": {"chan": 100.0}})
    assert resp.status_code == 400
    assert "未知因子" in resp.json()["detail"]


def test_empty_profile_is_rejected(client) -> None:
    resp = client.put(f"/api/v1/intraday/weight-profiles/{CODE}", json={"note": "只有备注"})
    assert resp.status_code == 400
    assert "档案为空" in resp.json()["detail"]


@pytest.mark.parametrize("bad_code", ["30030", "abc123", "830799"])
def test_invalid_code_is_rejected(client, bad_code: str) -> None:
    assert client.get(
        f"/api/v1/intraday/weight-profiles/{bad_code}").status_code == 400


def test_profile_repo_unavailable_degrades_to_503(tmp_dir: str) -> None:
    """档案仓储没装配时，档案接口应 503 而不是 500 —— 做T主链路仍可用。"""
    service = IntradayService(profile_repo=None)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(intraday=service, intraday_profile_repo=None)
    with TestClient(app) as test_client:
        assert test_client.get(
            "/api/v1/intraday/weight-profiles").status_code == 503
        # 但因子目录不依赖仓储，照常可用
        assert test_client.get(
            "/api/v1/intraday/factors").status_code == 200


# ==================== 预览：基准档位 ====================


def test_baseline_levels_uses_only_known_price_keys() -> None:
    """前端回传的「面板此刻档位」只取四个价格键，其余一律忽略。

    这条防线是必要的：`current_levels` 会被直接 model_copy 进档位对象，
    若把 `price`/`box_low`/`stop_loss_pct` 之类的键也放进去，用户就能用
    一个预览请求把基准篡改成任意数字（对照表会说"你的止损之前是 1 块钱"）。
    """
    from src.api.routes.intraday_weights import _baseline_levels
    from src.intraday.config import IntradayConfig
    from src.intraday.engine import compute_levels

    config = IntradayConfig()
    levels = compute_levels(
        price=10.0, box_high=10.3, box_low=9.7, box_span_days=20, config=config)
    baseline = _baseline_levels(
        {
            "low_buy": 9.5, "high_sell": "纳尼", "stop_loss": 9.2,
            "vwap": 9.9, "stop_loss_pct": 99.0, "price": 1.0,
        },
        levels)
    assert baseline is not None
    assert baseline.low_buy == pytest.approx(9.5)
    assert baseline.stop_loss == pytest.approx(9.2)
    assert baseline.vwap == pytest.approx(9.9)
    # 非数值 / 未在白名单里的键都不能生效
    assert baseline.high_sell == pytest.approx(levels.high_sell)
    assert baseline.stop_loss_pct == pytest.approx(levels.stop_loss_pct)
    assert baseline.price == pytest.approx(levels.price)


def test_baseline_levels_returns_none_without_usable_input() -> None:
    from src.api.routes.intraday_weights import _baseline_levels
    from src.intraday.config import IntradayConfig
    from src.intraday.engine import compute_levels

    levels = compute_levels(
        price=10.0, box_high=10.3, box_low=9.7, box_span_days=20,
        config=IntradayConfig())
    assert _baseline_levels({}, levels) is None
    assert _baseline_levels({"low_buy": "x"}, levels) is None


def test_preview_rejects_unknown_factor_names(client) -> None:
    """预览的权重也走同一套因子校验（坏口径不允许进打分链路）。"""
    resp = client.post("/api/v1/intraday/weight-profiles/preview",
                       json={"code": CODE, "mode": "intraday",
                             "weights": {"boll_band": 100.0}})
    assert resp.status_code == 400
    assert "未知因子" in resp.json()["detail"]


def test_preview_requires_at_least_one_change(client) -> None:
    resp = client.post("/api/v1/intraday/weight-profiles/preview",
                       json={"code": CODE, "mode": "intraday"})
    assert resp.status_code == 400
    assert "至少要给出一项改动" in resp.json()["detail"]


def test_preview_declares_current_levels_field() -> None:
    """`current_levels` 必须存在于请求契约里 —— 面板的「改动前」那一列靠它。"""
    from src.api.routes.intraday_weights import PreviewRequest

    assert "current_levels" in PreviewRequest.model_fields
    body = PreviewRequest.model_validate({
        "code": CODE, "mode": "intraday", "levels": {"stop_loss_pct": 2.0},
        "current_levels": {"low_buy": 9.5},
    })
    assert body.current_levels == {"low_buy": 9.5}


# ======================================================================
# /intraday/level-fit（档位拟合）：**quote 是 3 元组，必须解包**
# ======================================================================

class _Quote:
    """够用的行情对象：路由链路只读 `.price`。"""

    price = 89.36


class _StubProvider:
    """只喂 level-fit 路由要的两样：多日 bars 与实时快照。

    ⚠️ `fetch_quote` **必须返回 3 元组** —— 真实 `IntradayDataProvider` 的签名是
    `-> tuple[Quote, str, list[SourceAttempt]]`（多源容灾链）。
    这里刻意照抄那个形状，否则测试就钉不住这次的 bug。
    """

    def __init__(self, quote_result: tuple) -> None:
        self._quote_result = quote_result

    async def fetch_bars(self, code: str, *, days: int):
        return [object()], "腾讯行情", []          # 非空即可（路由只判空）

    async def fetch_quote(self, code: str):
        return self._quote_result


class _StubService:
    """只实现 level-fit 路由用到的那几个成员。"""

    def __init__(self, provider: _StubProvider, config) -> None:
        self.data_provider = provider
        self.config = config
        self._intraday_days = 10
        self.seen: dict = {}

    async def _fetch_daily_bars(self, code: str):
        return None, "router"                       # 本测试不关心日线

    async def level_fit(self, code, *, refresh, features, daily_bars, quote):
        # ★ 这里记录路由**实际传进来**的 quote —— 旧代码传的是元组，会在此暴露。
        self.seen = {"code": code, "refresh": refresh, "quote": quote}
        return LevelFitResult(code=code, trade_date="2026-09-23")


def test_level_fit_route_unpacks_quote_tuple(monkeypatch) -> None:
    """**回归（2026-09-23）**：点「重新拟合」不再 503。

    实测故障：`/intraday/level-fit` 把 `IntradayDataProvider.fetch_quote()` 的
    返回值**直接当成 Quote** 往下传。但那个方法返回的是 **3 元组**
    `(Quote, 源名, 尝试记录)`（多源容灾链），于是 `service._fit_levels_sync` 里
    的 `quote.price` 抛

        AttributeError: 'tuple' object has no attribute 'price'

    而 `level_fit` 把这个异常吞成 `None`，路由便报
    **503「档位拟合结果不可用」**——用户只看到一个语焉不详的失败。

    ⚠️ **指纹：只有 `refresh=true`（重新拟合按钮）必现**。
    `refresh=false` 时 `level_fit` 先命中当日缓存直接返回，根本走不到那个
    同步拟合函数；而那份缓存是**快照路径**用正确解包过的 Quote 写进去的。
    所以"不点重新拟合就正常、一点就炸"正是这个漏解包的指纹。

    同文件里 `fetch_bars` 那一行**是解了包的**（`bars, bars_source, _attempts =`），
    漏的只有 `fetch_quote` —— 这条测试把两者都钉住。
    """
    from src.api.routes import intraday_weights
    from src.intraday.config import load_intraday_config

    config = load_intraday_config()
    sentinel = _Quote()
    service = _StubService(_StubProvider((sentinel, "腾讯行情", [])), config)
    # 造真 bars 太重：特征工程不是本测试的对象，只要求它别炸。
    monkeypatch.setattr(intraday_weights, "build_intraday_features",
                        lambda bars, config=None: [{"fake": 1}])

    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(intraday=service)
    with TestClient(app) as client:
        resp = client.get("/api/v1/intraday/level-fit",
                          params={"code": CODE, "refresh": "true"})

    assert resp.status_code == 200, resp.text
    quote = service.seen["quote"]
    assert not isinstance(quote, tuple), \
        "路由把 fetch_quote 的 3 元组当 Quote 传下去了（refresh 路径会 503）"
    assert quote is sentinel, "解包后应当拿到元组里的那个 Quote 对象"
    assert service.seen["refresh"] is True, "refresh 没有透传到 level_fit"
