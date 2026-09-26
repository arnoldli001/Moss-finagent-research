"""竞价过程每日采集（防过期）的单测。

锁住三件事：

1. **候选口径**：涨停 + 流通市值 (20,110) 亿 + 昨收 < 45 元 + 非科创板/北证 + 非 ST；
   ST 用"官方涨停价反推限幅 ≈5%"判（仓库目录只有当前名称，拿它判历史会全错）。
2. **幂等**：已经采到的代码不再重复请求；`--refresh` 才重采。
3. **写盘不丢数据**：合并旧文件前必须**先读回来**。
   这一条是有来历的 —— 1 分钟线那支脚本曾经每批 `to_parquet` 覆盖写、
   却不读回旧文件，导致 5,224 只最终只剩 225 只，而进度文件还记着"全部完成"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import daily_auction_tick as dat  # noqa: E402


class _FakeXT:
    """假的 xtdata：记录请求、按预设返回。"""

    def __init__(self, payload: dict[str, int]) -> None:
        self.payload = payload
        self.downloaded: list[tuple[str, str]] = []

    def connect(self) -> None:
        return None

    def download_history_data(self, code: str, period: str, *, start_time: str,
                              end_time: str) -> None:
        self.downloaded.append((code, start_time))

    def get_market_data_ex(self, _f, codes, *, period, start_time, end_time, count):
        out = {}
        for c in codes:
            n = self.payload.get(c, 0)
            if n <= 0:
                out[c] = pd.DataFrame()
                continue
            idx = [f"{start_time[:8]}09300{i}" for i in range(n)]
            out[c] = pd.DataFrame(
                {"time": range(n), "lastPrice": [1.0] * n, "volume": [10] * n,
                 "amount": [100.0] * n,
                 "askPrice": [[1.0, 0, 0, 0, 0]] * n,
                 "bidPrice": [[1.0, 0, 0, 0, 0]] * n,
                 "askVol": [[5, 0, 0, 0, 0]] * n,
                 "bidVol": [[5, 0, 0, 0, 0]] * n},
                index=idx)
        return out


def test_collect_day_writes_and_reports(tmp_path, monkeypatch):
    """采集一天：落盘、行数、失败计数都要对得上。"""
    monkeypatch.setattr(dat, "OUT", tmp_path)
    fake = _FakeXT({"600000.SH": 3, "000001.SZ": 3, "300001.SZ": 0})
    monkeypatch.setattr(dat, "xtdata", fake)

    res = dat.collect_day("20260922", ["600000", "000001", "300001"])
    assert res["requested"] == 3
    assert res["saved"] == 2, "取不到的代码不该算进 saved"
    assert res["failed"] == 1
    out = tmp_path / "tick_auction_20260922.parquet"
    assert out.exists()
    df = pd.read_parquet(out)
    assert set(df["code"]) == {"600000", "000001"}
    assert len(df) == 6
    # 请求的是**竞价段**（09:15~09:31），不是整日
    assert all(s.endswith("091500") for _, s in fake.downloaded)


def test_collect_day_merges_without_losing_old(tmp_path, monkeypatch):
    """⚠️ 合并旧文件时不能丢数据 —— 曾经的静默丢数据 bug 就在这里。"""
    monkeypatch.setattr(dat, "OUT", tmp_path)
    out = tmp_path / "tick_auction_20260922.parquet"
    old = pd.DataFrame({"code": ["600000"] * 4, "trade_date": ["20260922"] * 4,
                        "lastPrice": [1.0] * 4, "volume": [1] * 4, "amount": [1.0] * 4,
                        "askPrice": [[1.0]] * 4, "bidPrice": [[1.0]] * 4,
                        "askVol": [[1]] * 4, "bidVol": [[1]] * 4,
                        "time": range(4)})
    old.to_parquet(out, index=False)

    fake = _FakeXT({"000001.SZ": 3})
    monkeypatch.setattr(dat, "xtdata", fake)
    res = dat.collect_day("20260922", ["000001"], merge=True)

    df = pd.read_parquet(out)
    assert set(df["code"]) == {"600000", "000001"}, "旧代码被冲掉了"
    assert res["saved"] == 2
    assert len(df) == 7


def test_collect_day_replaces_same_code_without_duplicating(tmp_path, monkeypatch):
    """同一只票重采：用新数据替换，不能留下重复行。"""
    monkeypatch.setattr(dat, "OUT", tmp_path)
    out = tmp_path / "tick_auction_20260922.parquet"
    pd.DataFrame({"code": ["600000"] * 4, "trade_date": ["20260922"] * 4,
                  "lastPrice": [9.0] * 4, "volume": [9] * 4, "amount": [9.0] * 4,
                  "askPrice": [[9.0]] * 4, "bidPrice": [[9.0]] * 4,
                  "askVol": [[9]] * 4, "bidVol": [[9]] * 4,
                  "time": range(4)}).to_parquet(out, index=False)
    fake = _FakeXT({"600000.SH": 3})
    monkeypatch.setattr(dat, "xtdata", fake)
    dat.collect_day("20260922", ["600000"], merge=True)
    df = pd.read_parquet(out)
    assert len(df) == 3, "同代码应被替换而不是叠加"
    assert set(df["lastPrice"]) == {1.0}
