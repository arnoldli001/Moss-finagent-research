"""`factor_ic_report.load_scores` 的 `selection.rank_key` 派生列测试。

## 为什么需要

落库的 `total` 用的是**兑现后**的 `gate_bonus`（只有约 12% 的候选板块拿到
那 15~20 分），于是它像"平滑分数 + 稀疏跳变"，用它算秩相关会**系统性低估**
排序质量（V2.2 实测：`base_total` H=20 IC +0.075/+0.085 对 `total`
+0.052/+0.040）。所以 `load_scores` 额外派生

    selection.rank_key = base_total + bonus_potential + etf_bonus

也就是**真正的排序键**。这里钉住两条不变式：

1. 有 `bonus_potential` 时，`rank_key` 必须**逐行精确**等于三项之和；
2. **没有**该字段的老数据里，**不得**生成这一列 —— 用 `gate_bonus` 兜底
   会让它静默退化成 `total`，"看起来有数据其实没测到"，比缺列更危险。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import factor_ic_report as report  # noqa: E402


def _make_db(path: Path, payloads: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE mainline_score"
                 "(trade_date TEXT, board_code TEXT, total REAL, payload TEXT)")
    for index, payload in enumerate(payloads):
        conn.execute(
            "INSERT INTO mainline_score VALUES(?,?,?,?)",
            ("20260105", f"B{index}", float(payload.get("__total", 0.0)),
             json.dumps(payload, ensure_ascii=False)))
    conn.commit()
    conn.close()


def test_rank_key_is_base_plus_potential_plus_etf(tmp_path: Path,
                                                  monkeypatch) -> None:
    db = tmp_path / "moss.db"
    _make_db(db, [
        {"__total": 61.0, "base_total": 52.0, "bonus_potential": 17.0,
         "etf_bonus": 3.0, "candidate": True, "layers": []},
        {"__total": 80.0, "base_total": 80.0, "bonus_potential": 0.0,
         "etf_bonus": 0.0, "candidate": True, "layers": []},
    ])
    monkeypatch.setattr(report, "MAIN_DB", db)
    _, dims = report.load_scores("20260101", "20260131")
    assert "selection.rank_key" in dims
    frame = dims["selection.rank_key"]
    assert frame.loc["20260105", "B0"] == pytest.approx(52.0 + 17.0 + 3.0)
    assert frame.loc["20260105", "B1"] == pytest.approx(80.0)
    # 第一行的 total(61) 与排序键(72) 不同 —— 这正是"跳变被移除"的证据
    assert frame.loc["20260105", "B0"] != pytest.approx(61.0)


def test_rank_key_is_absent_when_the_field_was_never_written(
        tmp_path: Path, monkeypatch) -> None:
    """老数据没有 `bonus_potential` → **不生成**该列（宁缺勿假）。"""
    db = tmp_path / "moss.db"
    _make_db(db, [
        {"__total": 61.0, "base_total": 52.0, "gate_bonus": 0.0,
         "etf_bonus": 0.0, "candidate": True, "layers": []},
    ])
    monkeypatch.setattr(report, "MAIN_DB", db)
    _, dims = report.load_scores("20260101", "20260131")
    assert "selection.rank_key" not in dims


def test_layers_and_dimensions_still_parse(tmp_path: Path,
                                           monkeypatch) -> None:
    """加了派生列之后，原有的层/维度解析不能受影响。"""
    db = tmp_path / "moss.db"
    _make_db(db, [{
        "__total": 70.0, "base_total": 70.0, "bonus_potential": 0.0,
        "etf_bonus": 0.0, "candidate": True,
        "layers": [
            {"key": "six_dim", "score": 70.0, "dimensions": [
                {"key": "trading", "score": 40.0, "available": True},
                {"key": "macro", "score": 33.0, "available": False},
            ]},
            {"key": "accumulation", "score": 50.0, "dimensions": []},
        ],
    }])
    monkeypatch.setattr(report, "MAIN_DB", db)
    _, dims = report.load_scores("20260101", "20260131")
    assert dims["six_dim"].loc["20260105", "B0"] == pytest.approx(70.0)
    assert dims["six_dim.trading"].loc["20260105", "B0"] == pytest.approx(40.0)
    # `available=False` 的维度不进表（否则"没算"会被当成"算出来是 33 分"）
    assert "six_dim.macro" not in dims
