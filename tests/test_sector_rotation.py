"""行业轮动日报的离线单测（不打网络、不碰 Tushare）。

覆盖三类回归风险：
  1. **装配**：板块去重/分层/榜单选择（嵌套子行业不能重复出现）；
  2. **研判**：规则结论必须与数字一致（普跌/普涨/风格差/连续性文案）；
  3. **渲染**：HTML 内嵌 JSON 不能被 `</script>` 截断，页面含固定免责声明。

2026-09-27 追加**慢页面修复**的回归守卫：
  · ECharts 本地化（CDN 地址在读取时被替换）；
  · 落伍报告"立刻回旧的 + 后台补生成"（不再同步等 20~40s）；
  · 后台生成失败进入冷却期（防失败重试风暴）。
"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from src.sector_rotation import service, store
from src.sector_rotation.report import DISCLAIMER, render_html


def _frame(rows: list[tuple[str, str, float, float, str]]) -> pd.DataFrame:
    """构造 moneyflow_ind_dc 形态的截面： (name, ts_code, pct, net_yi, content_type)"""
    return pd.DataFrame([
        {"name": name, "ts_code": code, "pct_change": pct,
         "net_amount": net * 1e8, "content_type": ctype,
         "trade_date": "20260924", "rank": 1}
        for name, code, pct, net, ctype in rows
    ])


def test_assemble_boards_dedupes_nested():
    frame = _frame([
        ("林业Ⅱ", "BK1255.DC", 5.83, 6.39, "行业"),
        ("林业Ⅲ", "BK1502.DC", 5.83, 6.39, "行业"),   # 与Ⅱ同值 → 去重
        ("煤炭", "BK0437.DC", 1.13, 3.29, "行业"),
        ("电子", "BK1201.DC", -2.47, -228.76, "行业"),
        ("某概念", "BK9999.DC", 9.99, 9.99, "概念"),   # 非行业 → 剔除
    ])
    trade_date, boards = service.assemble_boards(frame)
    assert trade_date == "20260924"
    names = [b["name"] for b in boards]
    assert "某概念" not in names
    assert names.count("林业Ⅱ") + names.count("林业Ⅲ") == 1
    # 按涨跌幅降序
    pcts = [b["pct"] for b in boards]
    assert pcts == sorted(pcts, reverse=True)
    assert boards[0]["name"] == "林业Ⅱ"  # 同值去重保留层级高的


def test_board_level():
    assert service.board_level("林业Ⅲ") == 3
    assert service.board_level("林业Ⅱ") == 2
    assert service.board_level("煤炭") == 1


def _payload(**overrides) -> dict:
    base = {
        "meta": {"trade_date": "20260924", "sources": []},
        "indices": [
            {"name": "上证指数", "price": 3888.37, "change_pct": -1.22, "amount_yi": 7836.0},
            {"name": "上证50", "price": 2842.74, "change_pct": -1.42, "amount_yi": 1027.0},
            {"name": "创业板指", "price": 3288.95, "change_pct": -2.68, "amount_yi": 4119.0},
        ],
        "market": {"turnover_yi": 16533.0, "turnover_delta_yi": -1140.0,
                   "up": 1084, "down": 4001, "limit_up": 52, "limit_down": 14},
        "market_flow": {"latest": {"date": "2026-09-24", "net_yi": -652.15},
                        "series": []},
        "heat": [
            {"name": "风电设备", "level": 2, "pct": 2.15, "net_yi": 6.97},
            {"name": "林业Ⅱ", "level": 2, "pct": 5.83, "net_yi": -1.0},
            {"name": "电子", "level": 1, "pct": -2.47, "net_yi": -228.76},
        ],
        "flow_1d": {
            "inflow": [{"name": "汽车零部件", "code": "BK0481.DC", "pct": -0.64, "net_yi": 9.9}],
            "outflow": [{"name": "电子", "code": "BK1201.DC", "pct": -2.47, "net_yi": -228.76}],
        },
        "flow_5d": [
            {"name": "汽车零部件", "code": "BK0481.DC", "net5_yi": -5.84},
        ],
        "industries": [],
    }
    base.update(overrides)
    return base


def test_narrative_declares_mood_and_flow():
    narrative = service.narrate(_payload())
    assert "普跌" in narrative["headline"]
    assert "-652.1亿" in narrative["headline"]
    # 上涨家数远少于下跌 → 普跌；文案里要带具体数字
    assert "1084" in narrative["body"] and "4001" in narrative["body"]


def test_narrative_style_spread():
    # 上证50 -1.42 vs 创业板指 -2.68 → 差 1.26 < 1.5 → 均衡
    assert "均衡" in service.narrate(_payload())["headline"]
    # 拉大差值 → 价值占优
    payload = _payload()
    payload["indices"][1]["change_pct"] = 1.0
    payload["indices"][2]["change_pct"] = -2.0
    assert "大盘价值占优" in service.narrate(payload)["headline"]


def test_narrative_continuity_uses_5d():
    narrative = service.narrate(_payload())
    inflow_view = narrative["views"][0]
    # 汽车零部件当日流入但 5 日累计为负 → 规则应提示"按单日反弹对待"
    assert "单日反弹" in inflow_view["text"]


def test_narrative_hot_without_money():
    # 林业Ⅱ 涨 5.83% 但主力净流出 → 情绪博弈方向
    narrative = service.narrate(_payload())
    watch = narrative["views"][-1]
    assert "情绪博弈" in watch["title"]
    assert "林业Ⅱ" in watch["title"]


def test_render_html_contains_data_and_disclaimer():
    html = render_html(_payload())
    assert "行业轮动与资金流向监控" in html
    assert DISCLAIMER in html
    assert "汽车零部件" in html            # 数据确实嵌进去了
    assert "rule-based-v1" in html         # 规则生成声明


def test_render_html_escapes_script_close():
    payload = _payload()
    payload["industries"] = [{"name": "x</script><script>alert(1)", "level": 1,
                              "pct": 1.0, "net_yi": 1.0, "code": "BK0000.DC"}]
    html = render_html(payload)
    assert "x</script><script>alert(1)" not in html


def test_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "store_dir", lambda: tmp_path)
    payload = _payload()
    store.save("2026-09-24", payload, "<html>demo</html>")
    assert store.load("20260924")["market"]["up"] == 1084
    assert store.load_html("2026-09-24") == "<html>demo</html>"
    assert store.history() == ["20260924"]
    assert store.latest_date() == "20260924"
    assert store.load("20990101") is None


@pytest.mark.asyncio
async def test_generate_uses_cached_report(monkeypatch, tmp_path):
    """已落盘且交易日不落伍、force=False → 不再取数（守护"一天一份"的幂等）。"""
    monkeypatch.setattr(store, "store_dir", lambda: tmp_path)
    store.save("20260924", _payload(), "<html/>")
    monkeypatch.setattr(service, "expected_trade_date", lambda: "20260924")

    async def _boom(*args, **kwargs):  # 若被调用说明穿透了缓存
        raise AssertionError("不该重新取数")

    monkeypatch.setattr(service, "fetch_index_quotes", _boom)
    result = await service.generate(force=False)
    assert result["meta"]["trade_date"] == "20260924"


def test_is_stale_rules(monkeypatch, tmp_path):
    """落伍判定：无缓存→过期；日历判不了→当新鲜；落盘 < 应有→过期。"""
    monkeypatch.setattr(store, "store_dir", lambda: tmp_path)
    monkeypatch.setattr(service, "expected_trade_date", lambda: "20260924")
    assert service.is_stale() is True                       # 一份都没有

    store.save("20260924", _payload(), "<html/>")
    assert service.is_stale() is False                      # 刚好是最新

    monkeypatch.setattr(service, "expected_trade_date", lambda: "20260925")
    assert service.is_stale() is True                       # 关机错过调度 → 落伍

    monkeypatch.setattr(service, "expected_trade_date", lambda: "")
    assert service.is_stale() is False                      # 日历判不了 → 不盲刷


@pytest.mark.asyncio
async def test_generate_heals_stale_cache(monkeypatch, tmp_path):
    """落盘落伍（关机错过 15:40）→ generate 自动重取，产出应有交易日的报告。"""
    monkeypatch.setattr(store, "store_dir", lambda: tmp_path)
    store.save("20260923", _payload(), "<html/>")
    monkeypatch.setattr(service, "expected_trade_date", lambda: "20260924")

    async def _idx():
        return {}

    async def _flow():
        return {"series": [], "breadth": {}}

    monkeypatch.setattr(service, "fetch_index_quotes", _idx)
    monkeypatch.setattr(service, "fetch_market_flow", _flow)
    monkeypatch.setattr(service, "fetch_sector_frame", lambda: _frame([
        ("煤炭", "BK0437.DC", 1.13, 3.29, "行业"),
    ]))
    monkeypatch.setattr(service, "fetch_flow_5d", lambda boards: {})

    result = await service.generate(force=False)
    assert result["meta"]["trade_date"] == "20260924"
    assert store.latest_date() == "20260924"                # 新报告已替换落盘


# ==================== 2026-09-27 慢页面修复的回归守卫 ====================

def test_localize_echarts_swaps_cdn(monkeypatch):
    """旧落盘报告里的 jsdelivr CDN 地址 → 本地同前缀地址（仅当本地文件可用）。"""
    from src.api.routes import sector_rotation as route

    html = ('<script src="https://cdn.jsdelivr.net/npm/echarts@5/'
            'dist/echarts.min.js"></script>')
    # 本地文件可用 → 替换为相对地址（iframe 同前缀解析到 /echarts.min.js）
    monkeypatch.setattr(route.report_mod, "echarts_src",
                        lambda: "echarts.min.js")
    assert 'src="echarts.min.js"' in route._localize_echarts(html)
    # 本地缺失（回退 CDN）→ 不动原文
    monkeypatch.setattr(route.report_mod, "echarts_src",
                        lambda: route._CDN_ECHARTS)
    assert route._localize_echarts(html) == html


def test_inject_refresh_banner_contains_poller():
    """落伍报告的横幅必须带 build_status 轮询 + seq 门槛 + 自动刷新。"""
    from src.api.routes import sector_rotation as route

    html = "<html><body><h1>报告</h1></body></html>"
    out = route._inject_refresh_banner(html, seq0=7)
    assert "正在后台生成" in out                     # 用户能看懂当前是旧报告
    assert "build_status" in out                     # 轮询端点（相对路径）
    assert "location.reload()" in out                # 建完自动刷新
    assert "s.seq > 7" in out                        # 只在"有新完成"时刷新
    # 注入点在 <body> 之后，且原内容保留
    assert out.index("<body>") < out.index("正在后台生成")
    assert "<h1>报告</h1>" in out


@pytest.mark.asyncio
async def test_stale_report_served_immediately(monkeypatch, tmp_path):
    """落伍时**不再同步等 20~40s**：立刻回旧报告 + 后台补生成（用户口径的
    "打开页面应该立即能加载到"）。"""
    from src.api.routes import sector_rotation as route

    monkeypatch.setattr(store, "store_dir", lambda: tmp_path)
    store.save("20260923", _payload(), "<html>old</html>")
    monkeypatch.setattr(service, "expected_trade_date", lambda: "20260924")
    assert service.is_stale() is True

    started = []

    async def _slow_generate(*, force=False):
        started.append(True)
        return _payload()

    monkeypatch.setattr(service, "generate", _slow_generate)
    monkeypatch.setattr(route, "_LAST_GEN_AT", 0.0)  # 不在冷却期

    payload = await route._latest_report()
    # 立刻拿到的是旧报告（带后台刷新标记），且后台任务已被发起
    assert payload["meta"]["trade_date"] == "20260924"   # _payload 的 meta
    assert payload["meta"]["background_refreshing"] is True
    # 让后台任务在本测试的事件循环内跑完（create_task 只是调度，
    # 需要让出循环它才会执行；fake generate 即时返回）。
    for _ in range(50):
        await asyncio.sleep(0)
        if not route._BUILDING["running"]:
            break
    assert route._BUILDING["running"] is False
    assert started == [True]      # 后台生成确实被 kick 且执行了恰好一次
    assert int(route._BUILDING["seq"]) >= 1


@pytest.mark.asyncio
async def test_generate_failure_enters_cooldown(monkeypatch, tmp_path):
    """后台生成失败 → 进入 15 分钟冷却（不每个请求都重试必然失败的取数）。"""
    from src.api.routes import sector_rotation as route

    monkeypatch.setattr(store, "store_dir", lambda: tmp_path)
    store.save("20260923", _payload(), "<html>old</html>")
    monkeypatch.setattr(service, "expected_trade_date", lambda: "20260924")

    async def _boom(*, force=False):
        raise RuntimeError("tushare 挂了")

    monkeypatch.setattr(service, "generate", _boom)
    monkeypatch.setattr(route, "_LAST_GEN_AT", 0.0)

    payload = await route._latest_report()      # 不抛：回旧报告
    assert payload["meta"]["background_refreshing"] is True
    for _ in range(50):
        await asyncio.sleep(0)
        if not route._BUILDING["running"]:
            break
    assert route._BUILDING["running"] is False
    assert route._BUILDING["error"]              # 失败被记录
    # 冷却期内再请求：直接回旧报告、不重复 kick
    kicks_before = route._BUILDING["seq"]
    payload2 = await route._latest_report()
    assert payload2["meta"]["trade_date"] == "20260924"
    assert route._BUILDING["seq"] == kicks_before
