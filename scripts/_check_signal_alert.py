"""在 dev 库上真跑一次「情报 → 告警」，验证闸门与去重是否按预期工作。

⚠️ 只跑 dev（`data/dev/moss_dev.db`），**不要**指向 pilot。用法：
    .venv\\Scripts\\python.exe scripts\\_check_signal_alert.py

## 2026-10-01 补的那一段：为什么必须把知识星球单列出来

改判据之前，这个脚本的输出是"1 个候选（一条政策快讯）"—— 看起来一切正常，
而**真正的问题正好被这个"正常"掩盖**：知识星球 15 条真实笔记全部 58 分，
`MIN_CREDIBILITY=74` 那道闸门让它们**一条都进不来**，脚本却不会说
"有一批条目被信度闸门挡掉了"。用户看到的是"不弹窗"，排查时看到的是
"候选只有 1 条，符合预期"。

所以补这一段：把知识星球的条目**单独统计**（进了几条、被什么挡住），
让"这个来源有没有活着"在脚本输出里一眼可见。
"""
import asyncio
import logging
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.disable(logging.WARNING)


async def main() -> int:
    from src.core.config import get_settings
    from src.domain.alerts.service import in_trading_window
    from src.domain.intel import alert_bridge
    from src.domain.intel.service import build_feed
    from src.infrastructure.connectors.intel_sources import sort_key

    _ = get_settings()          # 触发配置加载（缺失时早失败，别等到入库）
    print("交易时段（决定弹窗还是只入库）:", in_trading_window())

    feed = await build_feed(limit=500, group_undetermined=False)
    print("平铺池:", len(feed.items))

    zsxq = [x for x in feed.items
            if alert_bridge.is_bypass_source(x)]
    print(f"\n=== 知识星球（research_note，信度闸门已旁路）{len(zsxq)} 条 ===")
    alive = 0
    for x in zsxq:
        c = (x.get("credibility") or {}).get("score")
        reason = alert_bridge.reason_of(x)
        flag = "会弹" if reason.fires else "不弹"
        alive += 1 if reason.fires else 0
        inst = "/".join(reason.institutions_display()) or "-"
        print(f"  [{flag}] c{c} triggers={reason.triggers()} "
              f"方向={reason.direction or '-'} 机构={inst}")
        print(f"       {str(x.get('title'))[:56]}")
        if reason.describe():
            print(f"       理由：{reason.describe()}")
    print(f"  —— 会弹 {alive} 条 / 不弹 {len(zsxq) - alive} 条")

    hits = alert_bridge.select(feed.items)
    print(f"\n=== 候选（共 {len(hits)} 条；"
          f"知识星球按内容规则，其它来源 方向明确 + 可信度"
          f"≥{alert_bridge.MIN_CREDIBILITY}）===")
    for x in hits:
        t = x.get("tone") or {}
        c = x.get("credibility") or {}
        e = alert_bridge.build_event(x)
        a = alert_bridge.build_assessment(x, alert_bridge.reason_of(x))
        print(f"  [{t.get('tone')}] c{c.get('score')} {e.event_type.value:<7} "
              f"sent={a.sentiment:<8} risk={a.risk_score:<5} "
              f"opp={a.opportunity_score:<5} conf={a.confidence:.2f} "
              f"stocks={[s.name or s.code for s in a.affected_stocks]}")
        print(f"       {str(x.get('title'))[:56]}")
        print(f"       event_key={e.event_key} content_key={e.content_key}")
        if a.impact_path:
            print(f"       理由：{a.impact_path}")

    if not hits:
        print("   （无）")
        return 0

    # 用真实告警引擎判一遍（不落库、不推送）——确认"仅 high"这条闸门
    from src.domain.alerts.thresholds import AlertEngine

    engine = AlertEngine()
    print("\n=== 引擎判定（是否真的产出 high 级告警）===")
    for x in hits:
        a = alert_bridge.build_assessment(x, alert_bridge.reason_of(x))
        alert = engine.evaluate(alert_bridge.build_event(x), a)
        if alert is None:
            print(f"  ✗ 不出告警: {str(x.get('title'))[:44]}")
        else:
            print(f"  ✓ {alert.alert_type.value:<12} {alert.alert_level.value:<7} "
                  f"{str(alert.title)[:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
