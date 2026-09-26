"""事件分析两阶段LLM提示词（FR-5精简流水线：每次扫描≤2次网关调用）。

阶段一(medium)：批量分类/情感/实体/摘要；
阶段二(reasoning)：批量风险与机会打分、受影响个股、传导路径。
仅输出JSON，结论必须基于给定事件文本，不得编造数据。

## 两个阶段的输出都用 `json_schema` **语法级约束**（2026-09-26 加）

提示词里写"只输出JSON"对**本地小模型**不够：实测 qwen3:8b 在阶段一
三次里有一次返回 `{ }`（思考 token 花了 564 个，正文是个空对象）——
解析失败 → 整批事件退回本地关键词兜底（情感恒为 neutral、摘要为空）。

传 `json_schema` 后 Ollama 走**受约束解码**（`format=<schema>`），
结构由采样器保证：同一 prompt 实测三次全部返回合法结构。
这是"用本地模型省钱"能否成立的前提 —— 省了钱但结构不可靠，
等于把评估质量悄悄换成了关键词规则。

⚠️ schema 必须与 `build_*_prompt` 里写死的输出格式**逐字段一致**
（包括 `entities`）：它是**约束**而不是建议，schema 里没写的字段
模型就不会输出（实测漏掉 `entities` 时行业/公司/地区全部消失）。
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
    # ── 打分标尺（2026-09-24 加）────────────────────────────────────────
    # 为什么必须写死分档：原来只有一句「评分须克制」，实测 24 条真快讯的分布是
    # 风险 p50=0 / p90=10 / max=45，机会 p50=30 / p90=42 / max=45，
    # 把握度 p50=0.55 / max=0.70 —— 全部挤在低档，而门槛是 风险中≥60 /
    # 机会中≥65 / confidence≥0.70，于是 `fact_alerts` 永远 0 行。
    # 这不是"宁漏勿滥"，是标尺没定义：模型不知道 60 分该长什么样。
    "## 打分标尺（按「影响能否落到具体标的与可量化方向」定档，不按新闻热度；"
    "**必须用满区间**，把一切都压进 0~45 会让分档失去意义）\n"
    "risk_score：80-100=已发生的行业/公司级实质冲击（监管处罚、禁令、"
    "龙头业绩暴雷、核心客户流失）；60-79=有明确负面传导方向且能指到行业或个股"
    "（价格/订单/成本/融资/政策收紧）；40-59=负面但幅度有限或标的模糊"
    "（短期扰动、单一小公司）；10-39=泛泛表述、无具体标的或不可验证；"
    "0-9=与 A 股/港股标的无实质关系（海外花絮、宏观闲聊）。\n"
    "opportunity_score：同一把尺子、方向相反。\n"
    "confidence：0.85-1.00=官方/权威源且标的与影响明确；0.65-0.84=权威源但影响"
    "需推断，或影响明确但来源一般；0.40-0.64=二手转述、传导链长；"
    "<0.40=无法判断。\n"
    "例外：宏观日常数据日历（例行 CPI/PMI/装机量发布）默认两低分（均 ≤20、"
    "confidence ≤0.5）；只有实质性政策/业绩/行业催化才进 60 以上档位。"
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


#: 阶段一输出的**结构约束**（与下面 `build_stage1_prompt` 里的输出格式一致）。
STAGE1_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "event_type": {
                        "type": "string",
                        "enum": ["policy", "sector", "stock", "calendar"],
                    },
                    "sentiment": {
                        "type": "string",
                        "enum": ["positive", "negative", "neutral"],
                    },
                    "entities": {
                        "type": "object",
                        "properties": {
                            "industries": {"type": "array",
                                           "items": {"type": "string"}},
                            "companies": {"type": "array",
                                          "items": {"type": "string"}},
                            "regions": {"type": "array",
                                        "items": {"type": "string"}},
                        },
                        "required": ["industries", "companies", "regions"],
                    },
                    "summary": {"type": "string"},
                },
                "required": ["event_id", "event_type", "sentiment",
                             "entities", "summary"],
            },
        }
    },
    "required": ["events"],
}

#: 阶段二输出的结构约束。
#:
#: 刻意**不**在 schema 里写 `minimum/maximum`：越界评分要由
#: `analyzer._bounded` 按 FR-6 丢弃该事件（而不是在采样器里被悄悄夹到边界，
#: 那会把"模型乱打分"变成"看起来合法的满分"）。
STAGE2_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "risk_score": {"type": "number"},
                    "opportunity_score": {"type": "number"},
                    "confidence": {"type": "number"},
                    "affected_stocks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "impact": {
                                    "type": "string",
                                    "enum": ["positive", "negative", "mixed"],
                                },
                                "reason": {"type": "string"},
                            },
                            "required": ["name", "impact", "reason"],
                        },
                    },
                    "impact_path": {"type": "string"},
                },
                "required": ["event_id", "risk_score", "opportunity_score",
                             "confidence", "affected_stocks", "impact_path"],
            },
        }
    },
    "required": ["assessments"],
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
