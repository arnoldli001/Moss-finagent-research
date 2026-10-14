"""拥挤度「一键刷新」的**写权限闸门在路由层**的判据（2026-10-13 用户报障）。

## 报障现场

在对外 pilot 上点「全量刷新」，拿到的是：

    前端：连不上服务…（已持续约 18 秒）
    再过一会儿：刷新失败：PermissionError: 本实例不能写 crowding_shared：
                登记的写者=dev（更新数据的责任在它），本实例是 'pilot' → **只读**…

时间线（`data/run/backend.log`）：两次 `POST /refresh_all` 都是 **200**，
后台线程随即失败 —— 也就是说**闸门原来只在后台线程里**
（`db.get_db_connection(..., writable=True)`），前端先收到"任务已提交"，
再收到一段 Python 异常原文。

## 这组判据守什么

| # | 判据 | 防的失效 |
|---|---|---|
| ① | 非写者 → **409** + 机器可读码 + 人话 | 又回到"200 之后才发现不能写" |
| ② | 非写者 → **一个任务都不起**（行为判据） | 只改状态码、照样起线程，白跑一轮 |
| ③ | 写者 → 照常起任务（**反向判据**） | 闸门写成"恒拒"，把 dev 也一起挡死 |
| ④ | 前端按**码**判、终态不给重试 | 文案一改就静默失效 / 让用户点一个永不成功的按钮 |
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.api.routes import sector_crowding as route
from src.infrastructure.catalog.data_stores import WriteDecision

ROOT = Path(__file__).resolve().parents[2]
BAR = ROOT / "web" / "src" / "components" / "SectorCrowdingRefreshBar.tsx"


def _decision(allowed: bool) -> WriteDecision:
    return WriteDecision(
        store="crowding_shared", allowed=allowed,
        reason=("登记的写者=dev（更新数据的责任在它），本实例是 'pilot' → **只读**"
                if not allowed else "本实例就是登记的写者 dev"))


def _no_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """路由里那两步建表/清缓存不必真跑（判据只关心"有没有起任务"）。"""
    monkeypatch.setattr(route, "_ensure_tables", lambda: None)
    monkeypatch.setattr(route, "_invalidate_max_cache", lambda: None)


# ====================================================== ① 非写者：409 + 码 + 人话

def test_read_only_instance_gets_409_with_machine_code(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _no_side_effects(monkeypatch)
    monkeypatch.setattr(route, "_write_decision", lambda: _decision(False))

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(route.refresh_all(None, None))          # type: ignore[arg-type]

    err = excinfo.value
    assert err.status_code == 409, "非写者必须是 409（可判定的拒绝），不是 200"
    detail = err.detail
    assert isinstance(detail, dict), f"必须是结构化 detail，实际 {type(detail)}"
    assert detail["code"] == route.CROWDING_READ_ONLY
    # 人话里必须带**出路**：谁是写者 + 去哪改（AGENTS.md：拒绝要给出路）
    assert "dev" in detail["message"]
    assert "data_stores.yaml" in detail["message"]


# ====================================================== ② 非写者：不起任务（行为）

def test_read_only_instance_does_not_start_a_task(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 行为判据：拒绝时**一个线程都不该起**。

    只看状态码会漏掉最贵的那种写法 —— 返回 409 但照样
    `start_refresh_all()`（用户看不到 task_id，后台却在跑一轮几分钟的重活）。
    """
    _no_side_effects(monkeypatch)
    monkeypatch.setattr(route, "_write_decision", lambda: _decision(False))
    started: list[dict] = []
    monkeypatch.setattr(route.refresh, "start_refresh_all",
                        lambda **kw: started.append(kw) or {"task_id": "should-not-happen"})

    with pytest.raises(HTTPException):
        asyncio.run(route.refresh_all(None, None))          # type: ignore[arg-type]

    assert started == [], f"被拒了却仍然起了任务：{started}"
    assert "should-not-happen" not in route.CROWDING_READ_ONLY


# ====================================================== ③ 写者：照常起任务（反向）

def test_writer_instance_still_starts_the_task(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """反向判据：闸门不许把真正的写者（dev）也挡掉。"""
    _no_side_effects(monkeypatch)
    monkeypatch.setattr(route, "_write_decision", lambda: _decision(True))
    started: list[dict] = []
    monkeypatch.setattr(route.refresh, "start_refresh_all",
                        lambda **kw: started.append(kw) or {"task_id": "t1", "status": "running"})

    out = asyncio.run(route.refresh_all(None, None))        # type: ignore[arg-type]

    assert out["ok"] is True and out["task_id"] == "t1"
    assert len(started) == 1, "写者实例应当照常起任务"
    assert started[0]["pool_only"] is True, "默认仍然只刷关注板块池"


# ====================================================== ④ 前端按**码**判，且终态不给重试

def test_frontend_keys_off_the_code_not_the_copy() -> None:
    """前端必须按**机器可读的码**判"这一档是终态"，不能按文案判。

    文案会被"优化得更友好"，每一次都会让某个隐藏分支静默失效（本项目有先例：
    图形码判据用 `/captcha_required/.test(String(e))` 匹配中文错误消息 ⇒ 永不渲染）。
    """
    src = BAR.read_text(encoding="utf-8")
    assert "READ_ONLY_CODE" in src, "前端没有按码分支"
    assert "ApiError" in src, "没有从 ApiError 里取 code"
    # 终态错误不许给"重试"按钮（点一万次也不会成功）
    assert re.search(r"\{!fatal\s*&&", src), (
        "只读（终态）时仍然渲染了「重试」按钮 —— 那是让用户对着一个永不成功的按钮点")


def test_frontend_read_only_code_matches_backend() -> None:
    """★ 跨语言契约：前端那个字面量必须与后端 `CROWDING_READ_ONLY` 逐字一致。

    这是唯一一处"两边各写一份、没法自动同步"的地方 —— 漂移的症状是
    **前端又回到"给重试按钮 + 显示异常原文"**，而且没有任何报错。
    """
    m = re.search(r'const READ_ONLY_CODE = "([^"]+)"', BAR.read_text(encoding="utf-8"))
    assert m, "找不到前端的 READ_ONLY_CODE"
    assert m.group(1) == route.CROWDING_READ_ONLY, (
        f"前端 {m.group(1)!r} != 后端 {route.CROWDING_READ_ONLY!r}")
