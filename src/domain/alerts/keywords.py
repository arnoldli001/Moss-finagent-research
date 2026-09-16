"""事件预筛与分类关键词（规则降噪层，FR-4）。

快讯先经本表命中过滤再进LLM，控制token成本；
命中优先级：政策 > 板块 > 个股代码。日历类事件由采集器直接给type_hint。
"""

from __future__ import annotations

import os
import re

# 自定义关注词（ALERT_KEYWORDS 逗号分隔，空则仅用内置表；T1可配置）
CUSTOM_KEYWORDS: tuple[str, ...] = tuple(
    kw.strip() for kw in os.environ.get("ALERT_KEYWORDS", "").split(",")
    if kw.strip()
)

# 政策类触发词（部委/政策工具/监管动作）
POLICY_KEYWORDS: tuple[str, ...] = (
    "国务院", "发改委", "工信部", "财政部", "央行", "证监会", "商务部", "科技部",
    "国资委", "能源局", "药监局", "银保监", "金融监管总局", "中央", "政治局",
    "政策", "法规", "规划", "指导意见", "管理办法", "实施方案", "行动方案",
    "补贴", "退税", "减税", "降准", "降息", "加息", "LPR", "MLF", "逆回购",
    "关税", "制裁", "出口管制", "反垄断", "监管", "整治", "准入", "退市新规",
    "印花税", "专项债", "特别国债", "国常会", "白皮书",
)

# 板块/产业链触发词
SECTOR_KEYWORDS: tuple[str, ...] = (
    "半导体", "芯片", "晶圆", "封测", "光刻", "AI", "人工智能", "算力", "光模块",
    "CPO", "服务器", "液冷", "PCB", "HBM", "存储芯片", "消费电子", "数据中心",
    "大模型", "机器人", "固态电池", "锂电池", "钠电池", "光伏", "风电", "储能",
    "新能源汽车", "充电桩", "创新药", "医药", "医疗器械", "白酒", "煤炭", "钢铁",
    "有色", "稀土", "铜", "铝", "石油", "天然气", "房地产", "银行", "券商",
    "保险", "军工", "低空经济", "核聚变", "氢能", "国产替代", "先进封装",
    "智能驾驶", "自动驾驶", "算力中心",
)

# A股6位代码（允许带括号出现，如"中际旭创(300308)"）
_STOCK_CODE_RE = re.compile(r"(?<!\d)([036]\d{5})(?!\d)")


def has_policy_keyword(text: str) -> bool:
    return any(kw in text for kw in (*POLICY_KEYWORDS, *CUSTOM_KEYWORDS))


def has_sector_keyword(text: str) -> bool:
    return any(kw in text for kw in SECTOR_KEYWORDS)


def find_stock_codes(text: str) -> list[str]:
    """提取文本中出现的A股6位代码（去重保序）。"""
    seen: dict[str, None] = {}
    for m in _STOCK_CODE_RE.finditer(text or ""):
        seen.setdefault(m.group(1), None)
    return list(seen)


def is_relevant(text: str) -> bool:
    """快讯是否值得进入分析（命中任一触发词或含股票代码）。"""
    return bool(
        has_policy_keyword(text)
        or has_sector_keyword(text)
        or find_stock_codes(text)
    )
