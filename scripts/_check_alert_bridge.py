"""验证 alert_bridge 的闸门与字段映射。"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.domain.intel import alert_bridge as B  # noqa: E402


def it(tone: str, score: int, codes=(), ind: str = "") -> dict:
    return {
        "title": "某公司公告重大合同",
        "summary": "正文" * 40,
        "published_at": "2026-09-25 10:00:00",
        "source_alias": "newswire-cls",
        "content_hash": "h1",
        "codes": list(codes),
        "industry": ind,
        "credibility": {"score": score},
        "tone": {"tone": tone, "has_tone": tone in ("偏多", "偏空"),
                 "neutral": tone == "中性"},
    }


print("=== 闸门 ===")
for tone, score in [("偏多", 94), ("偏空", 84), ("偏多", 54), ("未定", 94),
                    ("中性", 94), ("偏多", 74), ("偏空", 74), ("偏多", 73)]:
    x = it(tone, score, codes=["600519"])
    a = B.build_assessment(x)
    line = (f"  {tone} c{score:<3} alertable={B.is_alertable(x)!s:<5} "
            f"dir={B.direction_of(x) or '-':<4}")
    if a:
        line += (f" sent={a.sentiment:<8} risk={a.risk_score:<5} "
                 f"opp={a.opportunity_score:<5} conf={a.confidence:.2f} "
                 f"etype={a.event_type}")
    else:
        line += " -> None"
    print(line)

print("\n=== 跨源同文合并 ===")
x1 = it("偏多", 94)
x1["source_alias"], x1["content_hash"] = "newswire-cls", "a"
x2 = it("偏多", 94)
x2["source_alias"], x2["content_hash"] = "newswire-em", "b"
e1, e2 = B.build_event(x1), B.build_event(x2)
print("  content_key 相同（跨源抑制生效）:", e1.content_key == e2.content_key)
print("  event_key 不同（来源级各自去重）:", e1.event_key != e2.event_key)

print("\n=== event_type 降级映射 ===")
for kw in ({"codes": ["600519"]}, {"ind": "银行"}, {}):
    x = it("偏多", 94, **kw)
    print(f"  {kw or '无代码无行业'} -> {B.build_event(x).event_type}")

print("\n=== 脱敏：source_name 是内部名，出接口才假名化 ===")
e = B.build_event(it("偏多", 94, codes=["600519"]))
print("  内部 source_name =", e.source_name)
print("  raw_data（白名单构造，必须为空）=", e.raw_data)
print("  source_url（必须为空）=", repr(e.source_url))
