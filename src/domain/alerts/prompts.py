"""事件分析两阶段LLM提示词（FR-5精简流水线：每次扫描≤2次网关调用）。

阶段一(medium)：批量分类/情感/实体/摘要；
阶段二(reasoning)：批量风险与机会打分、受影响个股、传导路径。
仅输出JSON，结论必须基于给定事件文本，不得编造数据。
"""

from __future__ import annotations

import json

from src.domain.alerts.models import Event

_CONTENT_LIMIT = 300  # 单事件送入LLM的正文字符上限（控token）

STAGE1_SYSTEM = (
    "你是中国二级市场事件分类引擎。对批量财经事件逐项判断：\n"
    "1. event_type 仅可选 policy(政策/监管/宏观)、sector(行业/板块)、"
    "stock(具体上市公司)、calendar(待公布经济数据/财报/停复牌/分红日程)；\n"
    "2. sentiment 仅可选 positive/negative/neutral，依据事件对相关资产的方向；\n"
    "3. entities 提取行业(industries)、公司全称或简称(companies)、地区(regions)，"
    "没有则空数组，禁止臆测；\n"
    "4. summary 用不超过60个汉字概括事件核心。\n"
    "只输出JSON，禁止输出思考过程或多余文字。"
)

STAGE2_SYSTEM = (
    "你是中国二级市场风险与机会评估引擎。基于给定事件，逐项输出：\n"
    "1. risk_score(0-100整数)：事件演化为下跌/合规/流动性风险的强度，"
    "无风险给0；\n"
    "2. opportunity_score(0-100整数)：事件驱动的上涨机会强度，无机会给0；\n"
    "3. confidence(0.00-1.00)：基于信息确定性、影响直接性的把握度；\n"
    "4. affected_stocks：最相关的0-3只A股，每项含 name(公司简称)、"
    "impact(positive/negative/mixed)、reason(不超过25字)，无具体标的给空数组；\n"
    "5. impact_path：不超过50字的传导链，如 政策补贴→需求放量→龙头业绩。\n"
    "评分须克制：宏观日常数据日历默认两低分；仅实质性政策/业绩/行业催化给高分。"
    "评估基于给定公开信息，输出仅供投研参考、不构成投资建议。"
    "输出紧凑单行JSON（对象间不要换行、不要缩进），"
    "严格按事件条数逐项输出，禁止输出思考过程或多余文字。"
)


def _event_brief(event: Event) -> dict:
    return {
        "event_id": event.event_id,
        "type_hint": event.event_type.value,
        "title": event.title[:120],
        "content": event.content[:_CONTENT_LIMIT],
        "source_name": event.source_name,
    }


def build_stage1_prompt(events: list[Event]) -> str:
    payload = json.dumps(
        {"events": [_event_brief(e) for e in events]}, ensure_ascii=False)
    return (
        f"待分析事件如下（共{len(events)}条）：\n{payload}\n\n"
        '输出格式：{"events":[{"event_id":"...","event_type":"policy",'
        '"sentiment":"neutral",'
        '"entities":{"industries":[],"companies":[],"regions":[]},'
        '"summary":"..."}]}'
    )


def build_stage2_prompt(
    events: list[Event], stage1: dict[str, dict],
) -> str:
    items = []
    for event in events:
        info = stage1.get(event.event_id, {})
        items.append({
            "event_id": event.event_id,
            "event_type": info.get("event_type", event.event_type.value),
            "title": event.title[:120],
            "content": event.content[:_CONTENT_LIMIT],
            "sentiment": info.get("sentiment", "neutral"),
            "industries": info.get("entities", {}).get("industries", []),
            "companies": info.get("entities", {}).get("companies", []),
        })
    payload = json.dumps({"events": items}, ensure_ascii=False)
    return (
        f"待评估事件如下（共{len(events)}条）：\n{payload}\n\n"
        '输出格式：{"assessments":[{"event_id":"...","risk_score":0,'
        '"opportunity_score":0,"confidence":0.0,'
        '"affected_stocks":[{"name":"...","impact":"positive",'
        '"reason":"..."}],"impact_path":"..."}]}'
    )
