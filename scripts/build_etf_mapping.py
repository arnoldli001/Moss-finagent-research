"""重建 ETF ↔ 概念板块 映射表（`configs/mainline_etf_mapping.yaml`）。

## 为什么必须重做这张表

实测（本次诊断，`scripts/_diag_etf_coverage.py`）：

    板块池（同花顺概念 885/886 前缀）        324 个
    能映射到 ETF 的板块                      7 个 = 2.2%
    映射表 53 个关键字指向的板块名            31 个**申万一级行业名**
                                             （医药生物/证券/电力设备/银行/半导体…）

也就是说：原表的靶子（申万一级行业）和实际的分析池（概念板块）**几乎不相交**。
ETF 确实被匹配到了，只是全部射到了池外 —— 第二层的 `etf` 维度对
317/324 的板块**永久不可用**，却仍然占着 100% 权重和里的 15%。

## 新口径的三条改变

1. **匹配键用 `fund_basic.benchmark`（跟踪指数名）而不是 ETF 名称。**
   benchmark 覆盖率 2955/2955 = 100%，且是规范指数名
   （`中证人工智能主题指数×100%`），比"某某主题ETF"的营销名稳定得多。
   名称作为**兜底**也一起扫。

2. **结果落成显式代码映射（`overrides`），不再依赖关键字。**
   需求是"每个板块最多留规模最大的 3 只" —— 这是一个**带全局约束的有限清单**，
   关键字匹配在结构上做不到（它只能表达"包含某词"，不能表达"只留前 3"）。
   容器里 `match()` 先查 `overrides` 再查 `keywords`，所以显式清单天然优先。

3. **规模口径**：`ml_etf.shares`（万份）× `close`（元）= 万元，`/10000` = 亿元；
   取最近 20 个可用交易日的**中位数**（单日可能异常）。
   `etf_share_size` 无权限，这是唯一可行的路径（见 `sync_etf` 的说明）。

## 别名为什么要人工给

机械规则只做"去掉 概念/板块/指数/主题/产业 后缀"，能覆盖
`人工智能→人工智能`、`机器人概念→机器人`；但 `家用电器→家电`、
`农业种植→现代农业`、`稀土永磁→稀土` 这类**换词**的必须人工声明 ——
ETF 跟踪的指数名不会包含板块名本身。

**跨板块重名的别名会被丢弃并报告**（例如"农业"若同时被 农业种植 和 乡村振兴
声明，就不给任何一方），避免靠字典遍历顺序决定归属。

## 用法

    # 1) 只看候选，不写文件、不联网
    python scripts/build_etf_mapping.py --report docs/ETF_BOARD_MAPPING.md
    # 2) 把候选里行情/份额不足的 ETF 补到 2023-10 起（联网）
    python scripts/build_etf_mapping.py --sync --start 20231001
    # 3) 定稿写回 configs/mainline_etf_mapping.yaml
    python scripts/build_etf_mapping.py --write
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.errors import BRIEF_TIGHT, brief  # noqa: E402
from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402

#: 板块名后缀：去掉后往往就是 ETF 跟踪指数里用的词（`机器人概念`→`机器人`）。
SUFFIXES = ("概念", "板块", "指数", "主题", "产业", "行业", "服务")

#: **必须保留**的 ETF（`{板块名: (短代码, ...)}`）。
#:
#: ## 为什么"规模最大的前 N 只"这条规则本身不够
#:
#: 实测：本模块整个 ETF 维度的需求起点是 2026-06~07「农业ETF易方达」
#: `562900.SH` 的历史级放量。但它的规模只有 **1.6 亿元**，在 `农业种植`
#: 的候选里排第 **5**：
#:
#:     1. 159825  18.6亿   富国中证农业主题ETF
#:     2. 512620  12.9亿   天弘中证农业主题ETF
#:     3. 516810   4.1亿   华夏中证农业主题ETF
#:     4. 516550   1.9亿   嘉实中证大农业ETF
#:     5. 562900   1.6亿   易方达中证现代农业主题ETF   ← 需求点名的就是它
#:
#: 也就是说：**只按规模取 top3，会把这个维度存在的理由本身筛掉。**
#: 规模大 ≠ 信号强 —— 一只 18 亿的宽基农业 ETF 份额常年不动，而 1.6 亿的
#: 主题 ETF 可能正在被集中申购。规模相关性高只是**统计上**的默认值，
#: 遇到具体事件必须能覆盖。
#:
#: 因此保留 `--top` 的默认行为（每题最多 N 只、按规模取），
#: 但允许这里**钉死**若干只：先放 pin，再用规模填满剩余名额。
PINS: dict[str, tuple[str, ...]] = {
    "农业种植": ("562900",),      # 农业案例的当事 ETF，规模仅 1.6 亿
}

#: 人工别名：**左侧是池内板块的真实名称**（必须与 `ml_board.name` 完全一致），
#: 右侧是它可能出现在跟踪指数名里的写法。
#:
#: 只列"机械去后缀拿不到"的换词情形；能靠后缀规则拿到的不要重复列，
#: 否则别名表会迅速腐化成第二份难维护的字典。
#:
#: ⚠️ **窄板块不要声明宽别名**。第一版踩过的坑：给 `乳业` 加了 `食品饮料`、
#: 给 `医药电商` 加了 `医药`、给 `智能医疗` 加了 `医疗` —— 结果是
#: `乳业` 板块拿到了天弘食品饮料 ETF、`医药电商` 拿到了沪深300医药卫生 ETF。
#: 第二版又踩了一次同类：给 `磷化工` / `氟化工概念` / `钛白粉概念` 都加了
#: `化工`，按"名字最短者优先"裁决后 **`磷化工` 拿到了华宝中证细分化工产业
#: 主题ETF**（一只宽基化工 ETF），于是 2026-07-06 那天它凭空多了 15 分 ETF 加分。
#: 一个宽 ETF 被窄板块"抢走"后，它对真正对应的宽板块就不可用了，
#: 而且抢走的还是错误信号。**宁可这个板块没有 ETF，也不要一个错的。**
ALIASES: dict[str, tuple[str, ...]] = {
    # ---- 农业（本次需求点名的场景）----
    "农业种植": ("现代农业", "农业"),
    "种业": ("种业",),
    "养殖业": ("养殖", "畜牧"),
    "猪肉": ("猪肉", "畜牧养殖"),
    "养鸡": ("鸡产业",),
    "农机": ("农业机械",),
    # ---- 消费 ----
    "家用电器": ("家电",),
    "白酒概念": ("白酒",),
    "啤酒概念": ("啤酒",),
    "乳业": ("乳业",),            # 不声明 `食品饮料`：那是另一个板块的靶子
    "食品饮料": ("食品饮料",),
    "旅游概念": ("旅游",),
    "免税概念": ("旅游",),
    "化妆品": ("美容护理",),
    "纺织服装": ("纺织服装",),
    "智能家居": ("家居",),
    # ---- 科技 ----
    "芯片概念": ("芯片", "半导体"),
    "半导体": ("半导体",),
    "消费电子概念": ("消费电子",),
    "人工智能": ("人工智能",),
    "机器人概念": ("机器人",),
    "云计算": ("云计算",),
    "大数据": ("大数据",),
    "区块链": ("区块链",),
    "数字货币": ("数字货币",),
    "网络安全": ("网络安全",),
    "国产软件": ("软件",),
    "游戏": ("游戏", "动漫游戏"),
    "元宇宙": ("元宇宙",),
    "虚拟现实": ("虚拟现实",),
    "5G概念": ("5G", "通信"),
    "光模块": ("光模块", "通信"),
    "CPO概念": ("光模块", "通信"),
    "算力": ("算力",),
    "信创": ("信创", "软件"),
    "数字经济": ("数字经济",),
    "物联网": ("物联网",),
    "工业互联网": ("工业互联网",),
    "智慧城市": ("智慧城市",),
    "华为概念": ("华为",),
    "苹果概念": ("苹果", "消费电子"),
    "小米概念": ("小米", "消费电子"),
    "特斯拉概念": ("特斯拉",),
    "宁德时代概念": ("电池", "新能源车"),
    "无人驾驶": ("智能驾驶", "智能汽车"),
    "汽车电子": ("汽车电子", "智能汽车"),
    "卫星导航": ("卫星", "北斗"),
    "军工": ("军工", "国防"),
    "军民融合": ("军工",),
    "大飞机": ("大飞机", "航空航天"),
    # ---- 医药 ----
    "创新药": ("创新药",),
    "医疗器械概念": ("医疗器械",),
    "中药": ("中药",),
    "生物疫苗": ("疫苗",),
    "医美概念": ("医美",),
    "基因测序": ("基因测序",),
    # ---- 新能源 / 周期 ----
    "新能源汽车": ("新能源车", "新能源汽车"),
    "锂电池概念": ("锂电池", "电池"),
    "光伏概念": ("光伏",),
    "风电": ("风电",),
    "氢能源": ("氢能",),
    "燃料电池": ("燃料电池", "氢能"),
    "储能": ("储能",),
    "稀土永磁": ("稀土",),
    "黄金概念": ("黄金产业", "黄金股票"),
    "小金属概念": ("有色金属",),
    "金属钴": ("有色金属",),
    "有色金属": ("有色金属",),
    "煤炭": ("煤炭",),
    "钢铁": ("钢铁",),
    "特钢概念": ("钢铁",),
    "化工": ("化工",),
    "碳纤维": ("新材料",),
    "石墨烯": ("新材料",),
    "建材": ("建材",),
    "PPP概念": ("基建",),
    "一带一路": ("基建", "建筑"),
    "房地产": ("房地产", "地产"),
    # ---- 金融 / 公用 ----
    "券商": ("证券", "券商"),
    "银行": ("银行",),
    "保险": ("保险",),
    "电力": ("电力",),
    "环保": ("环保",),
    "碳中和": ("碳中和", "低碳"),
    "国企改革": ("国企改革",),
    "中字头股票": ("央企", "国企"),
    "航运概念": ("航运",),
    "物流": ("物流",),
    "水利": ("水利",),
    "天然气": ("天然气",),
    "核电": ("核电", "核能"),
    "在线教育": ("教育",),
    "职业教育": ("教育",),
    "养老概念": ("养老",),
}

#: 明确不是"A 股概念板块"的 ETF：剔除后才谈得上"规模最大的前 3 只"。
#: - 债券/货币/REIT 与股票主题无关；
#: - 境外指数 ETF（标普/纳斯达克/日经…）反映海外市场，不是 A 股概念板块；
#: - **商品期货 ETF**（`159980` 有色金属期货、`159981` 能源化工期货）名字里带
#:   "期货"，会被 `期货概念` 板块抢过去 —— 但一个跟踪商品期货合约，一个跟踪
#:   期货公司股票，毫无关系；
#: - "联接"是**场外**联接基金（`162412 华宝中证医疗ETF联接-A`），份额申赎
#:   机制与场内 ETF 不同，不该混进来。
EXCLUDE_WORDS = (
    "REIT", "债", "货币", "国债", "政金", "城投", "短融", "存单", "同业存单",
    "豆粕", "原油", "白银", "商品", "期货", "联接",
    # 实物贵金属：`518880 华安易富黄金ETF`（1066 亿，全场最大）跟踪的是
    # `国内黄金现货价格(Au99.99合约)` —— 它持有实物金，不是黄金股票。
    # 不加这条，`黄金概念` 板块的前三名会被三只**实物金** ETF 占满
    # （1066 / 428 / 374 亿），而真正对应这个板块的
    # `中证沪深港黄金产业股票指数` ETF（140 / 61 / 17 亿）全部落选 ——
    # 拿金价的申赎去解释金矿股的行情，是两个资产类别。
    "现货", "上海金", "Au99", "SHAU", "集中定价",
    "标普", "纳斯达克", "日经", "德国", "法国", "美国", "亚太",
)

#: 场内 ETF 的代码前缀白名单。**这是过滤分级基金/LOF/场外指数基金的关键**：
#: 它们同样在 `fund_basic(market='E')` 名录里，靠名称完全区分不出来 ——
#:   `150269 招商中证白酒指数分级-A`   分级基金子份额，2020 年底已清理退市
#:   `161725 招商中证白酒指数-A`       场外指数基金
#:   `501057 汇添富中证新能源汽车产业指数(LOF)-A`  LOF
#: 它们的行情区间普遍止于 2020 年，放进 2023-10 起的回测里只会是"选中了但
#: 永远没数据" —— 而现象只是"这个板块的 ETF 维度不响"，极难排查。
#:
#: ⚠️ **这个白名单曾经漏了 `158/520/526/530/551` 共 130 只**（实测
#: `scripts/_diag_etf_prefix.py`），后果是 `158038 富国国证粮食产业ETF`
#: 被当成"非场内"剔除、`粮食概念`/`玉米` 两个板块的 ETF 加分直接失效。
#: 教训：**白名单要靠数据生成，不能靠印象手写** —— 判据是"名称含 ETF
#: 且不含 联接/LOF/分级/指数-A"，把全部 `ml_etf_meta` 过一遍就能得到。
ETF_PREFIXES = (
    "510", "511", "512", "513", "514", "515", "516", "517", "518", "519",
    "520", "526", "530", "551",                    # 补充（实测遗漏）
    "560", "561", "562", "563", "564", "565", "566", "567", "568", "569",
    "588", "589",                                   # 科创板 ETF
    "158", "159",                                   # 深市 ETF
)

#: 默认排除 QDII / 港股通类（`--allow-qdii` 可放开）。
#: 理由：A 股概念板块的异动应当由 A 股 ETF 确认；恒生/港股通 ETF 的资金
#: 反映的是港股，且规模往往更大，会系统性挤掉真正的 A 股 ETF。
QDII_WORDS = ("QDII", "港股通", "恒生", "香港", "中概", "H股")

HEADER = """\
# 主线挖掘 · ETF ↔ 板块映射表
#
# ⚠️ 本文件由 `scripts/build_etf_mapping.py` 生成（改动会在下次构建时被覆盖）。
#    要增减 ETF，请改脚本里的 `ALIASES` / `--top`，或直接把结论写进
#    `overrides` 并在脚本里同步说明。
#
# 用途：把「板块相关 ETF 的份额变化 / 成交额放量」折算成板块级的资金异动信号。
#
# ## 为什么是显式代码清单而不是关键字
#
# 上一版用 53 个关键字匹配 ETF 名称，但那些关键字的靶子是**申万一级行业名**
# （医药生物 / 证券 / 电力设备 / 银行 …），而分析池是 **324 个同花顺概念板块**
# —— 两个集合几乎不相交：实测只有 7/324 = 2.2% 的板块能命中 ETF。
# 现在改成"每个板块显式列出规模最大的最多 {top} 只 ETF"，原因有二：
#   1. 需求本身是"每板块最多 N 只"，这是**全局约束**，关键字表达不了；
#   2. 显式清单可逐行审查，出错时一眼看出来，而不是"某个词抢走了某只 ETF"。
#
# ## 匹配键
#
# 构建时用 `fund_basic.benchmark`（跟踪指数名，覆盖率 100%）+ ETF 名称做包含匹配，
# **取最长命中**；运行时只认下面的 `overrides`（`match()` 先查 overrides）。
#
# ## 规模口径
#
# `ml_etf.shares`（万份）× `close`（元）= 万元 → /10000 = 亿元，
# 取最近 20 个可用交易日的**中位数**。`etf_share_size` 无 Tushare 权限，
# 这是唯一可行的路径。
"""


def board_aliases(name: str) -> list[str]:
    """一个板块名可能出现在跟踪指数名里的所有写法（长→短）。"""
    out = {name}
    for suffix in SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            out.add(name[: -len(suffix)])
    out.update(ALIASES.get(name, ()))
    return sorted(out, key=len, reverse=True)


def build_matcher(boards: list[tuple[str, str]]
                  ) -> tuple[list[tuple[str, str, str]], list[str]]:
    """返回 `[(别名, 板块名, 板块代码)]`（按别名长度降序）与被丢弃的重名别名。

    ## 重名别名怎么裁决

    同一个别名被多个板块声明时（`军工` 被 `军工` 和 `军民融合` 都声明），
    交给字典遍历顺序决定归属是隐式行为 —— 出了问题没人能在映射表里看出来。

    但"一律丢弃"也不对，实测踩过：`有色金属` 被 `有色金属` / `小金属概念` /
    `金属钴` 三家声明，全丢的结果是**`有色金属` 板块连自己的名字都不能用了**；
    `消费电子` 同样被 `消费电子概念` / `小米概念` / `苹果概念` 拖累。

    因此按**包含关系**裁决，优先级从高到低：

        1. 别名就是某板块的名字（`军工` == `军工`）      → 归它
        2. 别名是某板块名字的子串（`消费电子` ⊂ `消费电子概念`）→ 归它
        3. 仍然多个候选                                  → 整条丢弃并报告

    规则 2 里若有多家满足，取**名字最短**的（最贴近别名的那家）；
    仍然并列才丢弃。
    """
    claims: dict[str, set[str]] = defaultdict(set)
    for code, name in boards:
        for alias in board_aliases(name):
            claims[alias].add(code)
    name_of = dict((code, name) for code, name in boards)
    dropped: list[str] = []
    pairs: list[tuple[str, str, str]] = []
    for alias, codes in claims.items():
        winner: str | None = None
        if len(codes) == 1:
            winner = next(iter(codes))
        else:
            exact = [c for c in codes if name_of.get(c) == alias]
            contained = [c for c in codes if alias in str(name_of.get(c, ""))]
            for tier in (exact, contained):
                if len(tier) == 1:
                    winner = tier[0]
                    break
                if len(tier) > 1:
                    shortest = sorted(tier, key=lambda c: len(name_of.get(c, "")))
                    if len(name_of.get(shortest[0], "")) < len(
                            name_of.get(shortest[1], "")):
                        winner = shortest[0]
                    break
        if winner is None:
            dropped.append(f"{alias} ← " + "、".join(
                sorted(name_of.get(c, c) for c in codes)))
            continue
        pairs.append((alias, name_of[winner], winner))
    pairs.sort(key=lambda item: (-len(item[0]), item[0]))
    return pairs, sorted(dropped)


def is_onsite_etf(code: str) -> bool:
    """是不是**场内 ETF**（按代码前缀白名单）。

    不用名称判断：分级基金/LOF/场外指数基金的名字里同样有"ETF"或"指数"，
    只有交易所代码段是可靠的。
    """
    return str(code or "")[:3] in ETF_PREFIXES


def match_one(code: str, name: str, benchmark: str,
              pairs: list[tuple[str, str, str]],
              allow_qdii: bool) -> tuple[str, str, str]:
    """返回 `(板块名, 板块代码, 命中的别名)`；未命中返回三个空串。"""
    if not is_onsite_etf(code):
        return "", "", ""
    text = f"{name} {benchmark}"
    if any(word in text for word in EXCLUDE_WORDS):
        return "", "", ""
    if not allow_qdii and any(word in text for word in QDII_WORDS):
        return "", "", ""
    for alias, board_name, board_code in pairs:
        if alias and alias in text:
            return board_name, board_code, alias
    return "", "", ""


def etf_sizes(store: MainlineDataStore, codes: list[str]) -> dict[str, float]:
    """`{代码: 规模中位数(亿元)}`，按最近 20 个可用交易日算。"""
    out: dict[str, float] = {}
    for code in codes:
        rows = store._read(  # noqa: SLF001 运维脚本，直接读本地仓
            "SELECT shares, close FROM ml_etf WHERE code = ?"
            " AND shares IS NOT NULL ORDER BY trade_date DESC LIMIT 20", (code,))
        values = [float(r["shares"]) * float(r["close"]) / 10000.0
                  for r in rows if r["shares"] and r["close"]]
        if values:
            values.sort()
            out[code] = values[len(values) // 2]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="重建 ETF↔板块 映射表")
    parser.add_argument("--top", type=int, default=3,
                        help="每个板块最多保留几只 ETF（按规模降序）")
    parser.add_argument("--start", default="20231001", help="--sync 的起始日")
    parser.add_argument("--end", default="", help="--sync 的结束日（默认最新）")
    parser.add_argument("--sync", action="store_true",
                        help="为候选 ETF 补行情/份额（联网）")
    parser.add_argument("--write", action="store_true",
                        help="写回 configs/mainline_etf_mapping.yaml")
    parser.add_argument("--allow-qdii", action="store_true",
                        help="允许 QDII/港股通类 ETF 参与")
    parser.add_argument("--report", default="",
                        help="把审查表写到这个路径（Markdown）")
    args = parser.parse_args()

    cfg = load_config()
    store = MainlineDataStore(config=cfg)
    end = args.end or str(store._read(  # noqa: SLF001
        "SELECT MAX(trade_date) d FROM ml_etf")[0]["d"] or "")

    boards = [(str(r["code"]), str(r["name"])) for r in store._read(  # noqa: SLF001
        "SELECT code, name FROM ml_board WHERE source = 'sector_crowding:list'"
        " ORDER BY code")]
    metas = [(str(r["code"]), str(r["name"] or ""), str(r["benchmark"] or ""))
             for r in store._read(  # noqa: SLF001
                 "SELECT code, name, benchmark FROM ml_etf_meta ORDER BY code")]
    print(f"板块池 {len(boards)} 个 / ETF 名录 {len(metas)} 只 / 数据截至 {end}")

    pairs, dropped = build_matcher(boards)
    print(f"别名 {len(pairs)} 条（重名丢弃 {len(dropped)} 条）")

    candidates: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    excluded = 0
    offsite = 0
    unmatched_etf = 0
    for code, name, benchmark in metas:
        if not is_onsite_etf(code):
            offsite += 1
            continue
        board_name, board_code, alias = match_one(
            code, name, benchmark, pairs, args.allow_qdii)
        if not board_code:
            text = f"{name} {benchmark}"
            if any(word in text for word in EXCLUDE_WORDS) or (
                    not args.allow_qdii
                    and any(word in text for word in QDII_WORDS)):
                excluded += 1
            else:
                unmatched_etf += 1
            continue
        candidates[board_code].append((code, name, alias))

    print(f"① 候选：{len(candidates)}/{len(boards)} 个板块命中 ETF"
          f"（{len(candidates) / max(1, len(boards)) * 100:.1f}%）")
    print(f"   非场内 ETF 剔除 {offsite} 只 / 品种排除 {excluded} 只 / "
          f"别名未命中 {unmatched_etf} 只")

    if args.sync:
        wanted = sorted({code for items in candidates.values()
                         for code, _, _ in items})
        print(f"\n② 同步 {len(wanted)} 只候选 ETF 的行情+份额"
              f"（{args.start} ~ {end}，按年切片）…")
        done = 0
        for code in wanted:
            result = store.sync_etf(codes=[code], start=args.start, end=end,
                                   chunk_years=1.0)
            done += 1
            if result.status != "ok":
                print(f"   [{done}/{len(wanted)}] {code} {result.status} "
                      f"{brief(result.message, BRIEF_TIGHT)}")
            elif done % 20 == 0:
                print(f"   [{done}/{len(wanted)}] 已同步")

    sizes = etf_sizes(store, sorted({code for items in candidates.values()
                                     for code, _, _ in items}))
    name_of_board = dict(boards)

    # ---------- 定稿：每板块按规模取 top N ----------
    #
    # ⚠️ **必须先把行情同步进来再排序**。`ml_etf_meta` 里有 2955 只，
    # 但本地有行情的只有几百只 —— 没有规模数据的候选如果参与排序，
    # 它们之间全是 0.0 的平局，名次由代码字符串顺序决定，
    # 结果就是"选中了 3 只永远不会有信号的 ETF"，而报告上看不出异常。
    # 因此：**没有行情的候选直接剔除**（它们本来也算不出信号）。
    chosen: dict[str, list[tuple[str, str, float]]] = {}
    dropped_no_data: list[str] = []
    pinned_report: list[str] = []
    pin_by_short = {short: board for board, shorts in PINS.items()
                    for short in shorts}
    for board_code, items in candidates.items():
        board_name = name_of_board.get(board_code, "")
        usable = [item for item in items if sizes.get(item[0], 0.0) > 0]
        if not usable:
            dropped_no_data.append(
                f"{board_code} {board_name}"
                f"（{len(items)} 只候选全部无行情）")
            continue
        ranked = sorted(usable, key=lambda item: -sizes.get(item[0], 0.0))
        # 先放 pin，再用规模填满剩余名额（总数仍受 --top 约束）
        wanted = {short for short, board in pin_by_short.items()
                  if board == board_name}
        picked: list[tuple[str, str, float]] = []
        for code, name, _alias in ranked:
            if code.split(".")[0] in wanted:
                picked.append((code, name, sizes[code]))
                pinned_report.append(f"{board_name}: {code} {name} "
                                     f"{sizes[code]:.1f}亿")
        for code, name, _alias in ranked:
            if len(picked) >= args.top:
                break
            if code.split(".")[0] in wanted:
                continue
            picked.append((code, name, sizes[code]))
        missing_pins = wanted - {code.split(".")[0] for code, _, _ in picked}
        for short in sorted(missing_pins):
            dropped_no_data.append(
                f"pin 未生效：{board_name} 的 {short} 不在候选或没有行情")
        chosen[board_code] = picked[: args.top]

    print(f"\n③ 定稿：{len(chosen)} 个板块 / "
          f"{sum(len(v) for v in chosen.values())} 只 ETF"
          f"（{len(dropped_no_data)} 个板块因候选全无行情而放弃）")
    if pinned_report:
        print(f"   钉死保留 {len(pinned_report)} 只：")
        for item in pinned_report:
            print(f"     · {item}")
    covered_codes = {code for items in chosen.values()
                     for code, _, _ in items}
    if covered_codes:
        pool_sizes = sorted(sizes.get(c, 0.0) for c in covered_codes)
        print(f"   选中 ETF 规模：合计 {sum(pool_sizes):.0f} 亿元 / "
              f"中位 {pool_sizes[len(pool_sizes) // 2]:.1f} 亿元")
    else:
        print("   选中 ETF：无")

    # ---------- 报告 ----------
    lines: list[str] = []
    lines.append("# ETF ↔ 概念板块 映射审查表\n")
    lines.append(f"- 生成命令：`python scripts/build_etf_mapping.py "
                 f"--top {args.top}{' --allow-qdii' if args.allow_qdii else ''}`\n")
    lines.append(f"- 数据截至：{end}；板块池 {len(boards)} 个；"
                 f"ETF 名录 {len(metas)} 只\n")
    lines.append(f"- **覆盖率：{len(chosen)}/{len(boards)} = "
                 f"{len(chosen) / max(1, len(boards)) * 100:.1f}%**"
                 f"（上一版为 7/324 = 2.2%）\n")
    lines.append(f"- 每板块上限 {args.top} 只；非场内 ETF 剔除 {offsite} 只；"
                 f"品种排除 {excluded} 只；别名未命中 {unmatched_etf} 只\n")
    if pinned_report:
        lines.append(f"- **钉死保留 {len(pinned_report)} 只**（规模排序会漏掉它们，"
                     f"见 `build_etf_mapping.PINS`）：\n")
        lines.extend(f"  - {item}" for item in pinned_report)
    lines.append("\n## 定稿映射\n")
    lines.append("| 板块代码 | 板块名 | 候选数 | 选中 ETF（代码 / 名称 / 规模亿元） |")
    lines.append("|---|---|---|---|")
    for board_code in sorted(chosen, key=lambda c: -max(
            [s for _, _, s in chosen[c]] or [0])):
        picked = chosen[board_code]
        cell = "<br>".join(f"{code} {name[:20]} **{size:.1f}**"
                           for code, name, size in picked)
        lines.append(f"| {board_code} | {name_of_board.get(board_code, '')} "
                     f"| {len(candidates[board_code])} | {cell} |")
    miss = [f"{code} {name}" for code, name in boards
            if code not in chosen]
    lines.append(f"\n## 未覆盖板块（{len(miss)} 个）\n")
    lines.append("这些板块在当前 ETF 名录里找不到对应的 A 股概念 ETF"
                 "（已排除 QDII/债券/REIT）。它们不会拿到 ETF 加分。\n")
    lines.append("` `".join(miss) if miss else "（无）")
    if dropped_no_data:
        lines.append(f"\n## 候选全无行情而放弃的板块（{len(dropped_no_data)} 个）\n")
        lines.append("这些板块有别名候选，但候选在本地都没有行情（未同步或已退市），"
                     "因此无法按规模排序、也算不出信号，**整体放弃**。"
                     "同步行情后再跑一次即可恢复。\n")
        lines.extend(f"- {item}" for item in dropped_no_data)
    if dropped:
        lines.append(f"\n## 重名别名（已整条丢弃，{len(dropped)} 条）\n")
        lines.extend(f"- {item}" for item in dropped)
    report = "\n".join(lines) + "\n"
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
        print(f"   审查表 → {args.report}")

    # ---------- 写回 mapping ----------
    if args.write:
        target = ROOT / "configs" / "mainline_etf_mapping.yaml"
        body = [HEADER.format(top=args.top)]
        body.append(f"\nversion: 2\n")
        body.append("updated_at: \"\"   # 由构建脚本按需填写，不参与逻辑\n")
        body.append("\n# 关键字段**故意留空**：本版全部走下面的显式清单。\n"
                    "# 留空后 `load_mapping` 不再报\"没有 keywords 段\"的缺口\n"
                    "# （已放宽为 keywords 与 overrides 至少有一段即可）。\n"
                    "keywords: []\n")
        body.append("\n# 显式代码映射：`[ETF 短代码, 板块名]`。\n"
                    "# 短代码 = 去掉 `.SH`/`.SZ` 后缀；板块名必须与 ml_board.name 完全一致。\n"
                    "overrides:\n")
        for board_code in sorted(chosen):
            board_name = name_of_board.get(board_code, "")
            body.append(f"  # ---- {board_code} {board_name} ----\n")
            for code, name, size in chosen[board_code]:
                short = code.split(".")[0]
                body.append(f"  - [{short}, {board_name}]"
                            f"   # {name} {size:.1f}亿\n")
        body.append("\n# 规模与流动性门槛（避免把\"迷你 ETF 的单日异动\"当成板块信号）\n"
                    "filters:\n"
                    "  # 最近一日成交额下限（**元**）。`ml_etf.amount` 落库时已从\n"
                    "  # Tushare 的「千元」换算成元；500 万元是个保守下限。\n"
                    "  min_amount: 5000000.0\n"
                    "  min_history: 30           # 至少要有多少根日线才参与计算\n")
        target.write_text("".join(body), encoding="utf-8")
        print(f"\n④ 已写入 {target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
