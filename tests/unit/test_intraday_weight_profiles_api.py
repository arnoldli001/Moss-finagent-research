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
