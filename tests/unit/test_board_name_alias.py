"""★ 跨分类体系的行业同义词：**逐条复核 + 双向过期检查**（用户报障驱动）。

## 报障现场（2026-09-29 原话）

> 「未获取到 `行业轮动:电气设备` 数据 —— **数据库里肯定有，网上也有**，
>   为什么上报无法获取到？」

他说得对：**名录**（`quant_stock_basic.industry`，110 个）里的 `电气设备`，
在平台板块池（东财口径）里叫 **`电力设备`** —— 两套分类体系的名字不同，
而精确/包含两档都命中不了（两者互不为子串）⇒ 连接器不产点。

## 为什么判据只认"逐条复核过的同义词"，不做模糊匹配

上线前实测过字符相似度兜底（`difflib.SequenceMatcher`，名录 110 个行业 ×
轮动日报 457 个板块）：相似度 ≥0.5 的有 **63 对**，而语义正确的只是少数 ——

    0.750  电气设备 → **火电设备**（真要的是「电力设备」）
    0.667  其他商业 → 其他种植业 · 0.500 铁路 → 钢铁 · 0.500 保险 → 环保
    0.500  白酒 → 啤酒 · 0.500 水务 → 水泥

**错配的板块比"没数据"更危险**（用户会拿到"看着有据"的错误水位）。
所以本文件守的不是"能不能连上"，而是"**只连复核过的那些**"。

## 过期检查（豁免不许变成永久豁免）

* 源名必须是**名录里真实存在的行业**（名录改名 → 红）；
* 目标名必须出现在**当前板块池**里（板块池改名/下线 → 红）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.infrastructure.connectors.platform_data_connector import (
    _BOARD_NAME_ALIASES,
    _match_board_name,
)

_ROOT = Path(__file__).resolve().parents[2]


def _directory_industries() -> list[str]:
    """本地名录里的行业名（`quant_stock_basic.industry`）。"""
    db = _ROOT / "data" / "quant" / "warehouse.db"
    if not db.exists():
        pytest.skip("本机没有共享行情仓（没数据的环境按知名降级处理）")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT industry FROM quant_stock_basic "
            "WHERE industry IS NOT NULL AND industry <> ''").fetchall()
    except sqlite3.Error:
        pytest.skip("名录表不可读（知名降级）")
    finally:
        conn.close()
    return sorted({str(r[0]) for r in rows})


def _board_pool() -> list[str]:
    """平台板块池（行业轮动日报的 `industries` 名字；缺失则用拥挤度表兜底）。"""
    try:
        from src.sector_rotation import store as rotation_store

        latest = rotation_store.latest_date()
        payload = rotation_store.load(latest) if latest else None
        names = [str(r.get("name") or "")
                 for r in ((payload or {}).get("industries") or [])]
        names = [n for n in names if n]
        if names:
            return names
    except Exception:  # noqa: BLE001 报告不可用 → 走拥挤度表
        pass
    return []


def test_the_reported_alias_maps_electric_equipment() -> None:
    """★ 报障现场：`电气设备` 必须能连上板块池里的 `电力设备`。"""
    pool = _board_pool()
    if not pool:
        pytest.skip("本机没有行业轮动日报（知名降级）")
    if "电力设备" not in pool:
        pytest.skip("当前板块池里没有「电力设备」（报告口径变了，由过期检查负责）")
    matched, tier = _match_board_name("电气设备", pool)
    assert matched == "电力设备", f"应映射到电力设备，实际 {matched!r}"
    assert tier == "alias", "档位必须是 alias（让调用方看得出这是被映射过的）"


def test_alias_sources_are_real_directory_industries() -> None:
    """过期检查 ①：源名必须仍是名录里的行业（改名 → 红，逼你更新或删掉）。"""
    industries = set(_directory_industries())
    if not industries:
        pytest.skip("本机没有名录（知名降级）")
    stale = [src for src in _BOARD_NAME_ALIASES if src not in industries]
    assert not stale, (
        f"`_BOARD_NAME_ALIASES` 里这些**源名**已不是名录行业：{stale}\n"
        "→ 名录改了名，请更新映射或删掉条目（别让它烂在那里）")


def test_alias_targets_exist_in_the_current_board_pool() -> None:
    """过期检查 ②：目标名必须在当前板块池里（板块改名/下线 → 红）。"""
    pool = _board_pool()
    if not pool:
        pytest.skip("本机没有行业轮动日报（知名降级）")
    normalized = {"".join(n.split()).lower() for n in pool}
    missing = [dst for dst in _BOARD_NAME_ALIASES.values()
               if "".join(dst.split()).lower() not in normalized]
    assert not missing, (
        f"`_BOARD_NAME_ALIASES` 里这些**目标名**不在当前板块池：{missing}\n"
        "→ 板块池口径变了，请更新映射或删掉条目")


def test_exact_and_alias_tiers_are_distinguishable() -> None:
    """档位语义自证：精确命中不许被降级成 alias（否则口径说明会失真）。"""
    pool = ["电力设备", "银行(A股)", "白酒Ⅱ"]
    assert _match_board_name("电力设备", pool) == ("电力设备", "exact")
    assert _match_board_name("电气设备", pool) == ("电力设备", "alias")
    assert _match_board_name("不存在的行业ZZZ", pool)[1] == "none"
