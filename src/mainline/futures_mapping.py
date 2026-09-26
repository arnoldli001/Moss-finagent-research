"""主线挖掘：期货品种 → A 股板块的传导映射表（需求 5.3，静态数据 + 季度校准）。

## 这张表是干什么的

需求 5.3 的链路是"期货先行异动 → 可能被带动的 A 股板块 → 再用板块评分去验证"。
第一段（期货价格异动）可以算，第二段（异动该落到哪个板块）**没有公式**："铜价涨影响
铜产业股"是产业常识而不是可推导的结果。所以这一段以**人工映射表**的形式固化在本模块，
结构契约是 `src.mainline.models.FutureMapping`；本模块只做两件事：把表交出去，以及按
季度用真实滚动相关性校准一次。

表内共 109 行 / 104 个品种：内盘 70 个（Tushare `fut_daily` 主力连续代码，`RB.SHF`/
`LC.GFE`，逐个实测有行情）、外盘 25 个（CBOT/NYMEX/COMEX/LME/ICE/伦敦金银行情 +
6 个海外指数）、非期货 9 个（汇率 4 + 美元指数 + 十年美债主连 + 2 个油气 LOF + 纳指
100ETF）。其中 3 行是**反向**传导（`negative`）：原油涨压航空利润、豆粕涨压养殖利润、
猪价涨压屠宰毛利 —— 同一品种对上下游方向相反，正是"看商品做股票"最容易做反的地方。

需求 5.1 的品种池是 89 个（55 内盘 + 25 外盘 + 9 非期货）。本表**不是**那个池的复制品：
需求 5.3 点名了一批兄弟品种（氧化铝、烧碱、纯苯、瓶片、硅铁/锰硅…），补进来"产业链级
异动"（同链 N 个品种同时异动）才判得出来；要严格按 89 池统计，用 `kind` 与 `future_code`
自行过滤即可。7 个内盘品种**故意不收录**：动力煤 `ZC.ZCE`、强麦 `WH.ZCE`、普麦
`PM.ZCE`、早籼稻 `RI.ZCE`、粳稻 `JR.ZCE` 近 35 个交易日成交量合计为 0，晚籼稻
`LR.ZCE` 已无行情，线材 `WR.SHF` 同期仅 606 手 —— 收进来只会得到一条不动的序列。

## 强度：人工设定，季度校准**只写旁边那一列**

`strength`（1-5 ★）由研究员按产业逻辑打，`calibrated_strength` 才是过去 250 个交易日
滚动相关系数的产物（口径见 `configs/mainline.yaml` 的 `futures.recalibration`）。
校准结果**不覆盖** `strength`：两列回答两个不同问题 —— 人工值回答"这条传导在产业上成
不成立"，校准值回答"过去一年它有没有真的发生"。把后者写进前者，等于让一段样本期把产业
逻辑改掉：某季度铜价与铜股因个别事件背离，人工口径就被静默抹掉且不留痕迹。取哪一个由
`effective_strength()` 决定（有校准值用校准值，否则回落人工值）。

## 三个必须知道的坑

**1. 传导方向会失效。** 映射是"通常成立"的经验规律而不是恒等式：政策限价（动力煤/
成品油）、价差结构反转（纯碱涨是玻璃的成本上升）、收储/配额/环保限产等供给端事件，都会
让"商品涨 → 板块涨"在某一阶段整体不成立。所以映射只用来**缩小观察范围**，不能当看多
理由；`negative`（反向）与 `auxiliary`（仅背景）的行，打分时必须区别对待。

**2. 外盘代码不是 Tushare 代码。** 同一列混了三种约定，靠 `kind` 区分取数路径：内盘走
`fut_daily` 主力连续；外盘商品用 AKShare/新浪外盘字母代码（`C`/`S`/`CL`/`GC`/`CAD`…），
海外指数用 `index_global` 代码（`IXIC`/`HSI`/`DJI`/`SPX`/`N225`/`GDAXI`，已实测有数据）；
非期货用 `fx_daily` 的 `USDCNH.FXCM` 与场内基金代码（`501018.SH`，已用 `fund_basic` 核对）。

**3. 同名不同物。** "苹果"既是郑商所苹果期货、也是 A 股"苹果概念"；`SI`（COMEX 白银）
与 `SI.GFE`（工业硅）、`C`（CBOT 玉米）与 `C.DCE`（玉米）、`SM`（CBOT 豆粕）与
`SM.ZCE`（锰硅）也只是字母相同。所以**内盘代码一律带交易所后缀**：`calibrate()` 把
`future_code` 当字典键，重名会把两个品种的相关系数混在一起。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import pandas as pd

from src.core.errors import BRIEF_TIGHT, brief
from src.mainline.models import FutureKind, FutureMapping

logger = logging.getLogger(__name__)

#: 校准窗口：与 `configs/mainline.yaml` 的 `futures.recalibration.window_days` 一致。
#: 这里再写一份而不解析 YAML，是因为本模块是纯内存静态表、import 时不该读盘；
#: 默认值只作签名兜底，真正跑季度校准时应由调用方从配置取 `window_days` 传入。
RECALIBRATION_WINDOW_DAYS = 250
#: 校准间隔（天）：90 ≈ 一个季度，调用方据此判断"是不是该重算了"。
RECALIBRATION_INTERVAL_DAYS = 90
#: |corr| 达到该值即视为满分 5 ★（再往上区分没有统计意义，只会放大噪音）。
_CORR_FULL_SCALE = 0.8
#: 参与相关性的最少样本点（低于它宁可不给校准值，也不给一个由 20 个点撑起来的数）。
_MIN_OBS_FLOOR = 20

# 表内单行字段顺序（缺省值让大部分行只写前 7 项）：
#   code, name, board, strength, lead_days, logic, chain[, direction, kind]
# direction 默认 positive，反向写 _NEG，辅助行写 _AUX；kind 默认 _DOM，外盘 _FOR，
# 非期货 _NON。宏观行的 chain 统一是「宏观」（因子桶，不是产业链），strength 固定 1。
_DOM = FutureKind.DOMESTIC
_FOR = FutureKind.FOREIGN
_NON = FutureKind.NON_FUTURES
_POS = "positive"
_NEG = "negative"
_AUX = "auxiliary"

#: 构建期发现的问题行（不在 import 时抛错：一张静态表写错一行，不该让整条链路起不来）。
TABLE_GAPS: list[str] = []


# ==================================================================
# 映射表
# ==================================================================

_RAW: tuple[tuple[Any, ...], ...] = (
    # ---------------- 贵金属 ----------------
    ("AU.SHF", "沪金", "黄金股", 5, 1, "金价决定矿企吨毛利，沪金与黄金股当日同向", "贵金属"),
    ("AG.SHF", "沪银", "白银概念", 5, 1, "银价弹性大于金价，白银股贝塔更高", "贵金属"),
    ("XAU", "伦敦金", "黄金股", 4, 1, "隔夜定盘价先于 A 股开盘，黄金股的锚", "贵金属", _POS, _FOR),
    ("XAG", "伦敦银", "白银概念", 4, 1, "与沪银同源，隔夜波动由内盘补涨", "贵金属", _POS, _FOR),
    ("GC", "COMEX黄金", "黄金股", 4, 1, "美盘是定价中心，影响次日开盘情绪", "贵金属", _POS, _FOR),
    ("SI", "COMEX白银", "白银概念", 4, 1, "同黄金，但受工业需求驱动波动更大", "贵金属", _POS, _FOR),
    ("PT.GFE", "铂", "贵金属", 2, 3, "铂族新品种，与黄金共享贵金属仓位", "贵金属"),
    ("PD.GFE", "钯", "贵金属", 2, 3, "钯主用于尾气催化，传导稀有金属情绪", "贵金属"),
    # ---------------- 有色金属 ----------------
    ("CU.SHF", "沪铜", "铜产业", 4, 2, "铜价影响矿企利润与冶炼加工费", "有色金属"),
    ("BC.INE", "国际铜", "铜产业", 3, 2, "不含税铜价，与沪铜价差反映内外需", "有色金属"),
    ("HG", "COMEX铜", "铜产业", 4, 2, "美盘铜价领先内盘，是铜股的隔夜锚", "有色金属", _POS, _FOR),
    ("CAD", "LME铜", "铜产业", 4, 2, "LME 是铜定价中心，库存升贴水定", "有色金属", _POS, _FOR),
    ("AL.SHF", "沪铝", "有色金属", 3, 2, "电解铝利润=铝价-氧化铝-电价", "有色金属"),
    ("AHD", "LME铝", "有色金属", 3, 2, "海外铝价影响出口报价与内外价差", "有色金属", _POS, _FOR),
    ("ZN.SHF", "沪锌", "有色金属", 3, 3, "锌价与冶炼加工费共同决定锌企利润", "有色金属"),
    ("ZSD", "LME锌", "有色金属", 2, 3, "外盘锌价影响锌企出口报价", "有色金属", _POS, _FOR),
    ("PB.SHF", "沪铅", "有色金属", 2, 3, "影响再生铅与铅酸电池成本", "有色金属"),
    ("NI.SHF", "沪镍", "有色金属", 3, 2, "牵动不锈钢成本与三元前驱体成本", "有色金属"),
    ("NID", "LME镍", "有色金属", 2, 3, "伦镍波动放大，隔夜异动传导快", "有色金属", _POS, _FOR),
    ("SN.SHF", "沪锡", "有色金属", 3, 2, "锡是半导体焊料刚需，与电子景气共振", "有色金属"),
    ("AO.SHF", "氧化铝", "有色金属", 3, 3, "电解铝第一大成本，与铝价反向", "有色金属"),
    ("AD.SHF", "铸造铝合金", "有色金属", 2, 3, "再生铝定价，反映汽车轻量化需求", "有色金属"),
    # ---------------- 黑色系 ----------------
    ("RB.SHF", "螺纹钢", "钢铁", 4, 3, "螺纹是地产基建需求读数，钢价定利润", "黑色系"),
    ("HC.SHF", "热卷", "钢铁", 4, 3, "热卷对应制造业需求，与螺纹共振", "黑色系"),
    ("I.DCE", "铁矿石", "钢铁", 3, 2, "铁矿是钢厂最大成本项，涨压钢厂利润", "黑色系"),
    ("J.DCE", "焦炭", "煤炭", 4, 3, "焦炭是焦化利润载体，煤焦钢链中间环节", "黑色系"),
    ("JM.DCE", "焦煤", "煤炭", 4, 3, "焦煤是焦炭成本，双焦同涨即链条级异动", "黑色系"),
    ("SS.SHF", "不锈钢", "特钢", 2, 3, "不锈钢价与镍铬成本共振，传导特钢", "黑色系"),
    ("SF.ZCE", "硅铁", "铁合金", 2, 3, "炼钢脱氧剂，反映钢厂开工与能耗成本", "黑色系"),
    ("SM.ZCE", "锰硅", "铁合金", 2, 3, "成本随锰矿与电价，涨则压钢厂利润", "黑色系"),
    # ---------------- 能源 ----------------
    ("SC.INE", "原油", "石油石化", 4, 2, "油价定上游利润与炼化价差，内盘锚", "能源"),
    ("SC.INE", "原油", "航空机场", 2, 2, "航油是航空最大成本项，涨压利润（反向）", "能源", _NEG),
    ("CL", "NYMEX原油", "石油石化", 4, 2, "WTI 是隔夜油价基准，石化股先看它", "能源", _POS, _FOR),
    ("OIL", "布伦特原油", "石油石化", 4, 2, "Brent 是现货基准，定成品油", "能源", _POS, _FOR),
    ("FU.SHF", "燃料油", "石油石化", 3, 3, "船燃与炼厂余料，跟原油但受船运扰动", "能源"),
    ("LU.INE", "低硫燃料油", "石油石化", 3, 3, "受限硫令与航运需求驱动，与集运共振", "能源"),
    ("BU.SHF", "沥青", "石油石化", 3, 3, "炼厂重油流向，旺季对应基建开工", "能源"),
    ("PG.DCE", "液化石油气", "石油石化", 2, 3, "绑定丙烷进口价与民用燃气需求", "能源"),
    ("NG", "NYMEX天然气", "燃气", 3, 3, "气价影响城燃采购成本与贸易商价差", "能源", _POS, _FOR),
    ("501018.SH", "南方原油LOF", "石油石化", 3, 1, "QDII 基金，跟油价", "能源", _POS, _NON),
    ("162411.SZ", "华宝油气LOF", "石油石化", 2, 1, "跟踪美股油气上游，看溢价", "能源", _POS, _NON),
    # ---------------- 新能源 / 建材 ----------------
    ("PS.GFE", "多晶硅", "光伏", 4, 5, "硅料价格决定光伏链条利润分配", "新能源"),
    ("SI.GFE", "工业硅", "光伏", 3, 5, "工业硅是多晶硅与有机硅共同原料", "新能源"),
    ("LC.GFE", "碳酸锂", "锂电池", 4, 3, "锂价影响锂矿利润与电池成本", "新能源"),
    ("LC.GFE", "碳酸锂", "能源金属", 3, 3, "矿端口径：锂矿收入直接由锂价决定", "新能源"),
    ("FG.ZCE", "玻璃", "建材", 3, 5, "玻璃价格定浮法利润，地产竣工端读数", "建材"),
    ("SA.ZCE", "纯碱", "建材", 3, 5, "玻璃第一大原料，涨则压玻璃股利润", "建材"),
    ("SA.ZCE", "纯碱", "光伏玻璃", 3, 5, "光伏玻璃扩产拉动重碱需求，同链", "建材"),
    # ---------------- 化工 / 橡胶 ----------------
    ("TA.ZCE", "PTA", "基础化工", 2, 3, "PX-PTA-聚酯价差是聚酯链利润度量", "化工"),
    ("PX.ZCE", "对二甲苯", "基础化工", 2, 3, "PX 是 PTA 原料，价差决定 PTA 利润", "化工"),
    ("PF.ZCE", "短纤", "基础化工", 2, 3, "聚酯终端，加工费反映纺织需求", "化工"),
    ("EG.DCE", "乙二醇", "基础化工", 2, 3, "决定聚酯成本端，进口依存度高", "化工"),
    ("PR.ZCE", "瓶片", "基础化工", 2, 3, "聚酯另一出口，加工费看饮料需求", "化工"),
    ("MA.ZCE", "甲醇", "基础化工", 2, 3, "煤化工与 MTO 成本核心，随煤价", "化工"),
    ("PP.DCE", "聚丙烯", "基础化工", 2, 3, "油头与煤头工艺价差，看包装需求", "化工"),
    ("L.DCE", "塑料", "基础化工", 2, 3, "LLDPE 是农膜与包装料，油头主导", "化工"),
    ("V.DCE", "PVC", "基础化工", 2, 3, "电石法/乙烯法价差，看地产管材", "化工"),
    ("EB.DCE", "苯乙烯", "基础化工", 2, 3, "纯苯-苯乙烯价差即 EPS/ABS 利润", "化工"),
    ("BZ.DCE", "纯苯", "基础化工", 2, 3, "芳烃链起点，与原油、苯乙烯联动", "化工"),
    ("PL.ZCE", "丙烯", "基础化工", 2, 3, "PP 上游，PDH 利润影响化工预期", "化工"),
    ("SH.ZCE", "烧碱", "基础化工", 2, 3, "氯碱平衡另一端，氧化铝需求主导", "化工"),
    ("UR.ZCE", "尿素", "化肥", 2, 3, "化肥主品种，气头成本与农需共振", "化工"),
    ("RU.SHF", "天然橡胶", "轮胎", 3, 2, "天胶占轮胎成本大头，涨则压毛利", "橡胶"),
    ("NR.INE", "20号胶", "轮胎", 3, 2, "轮胎专用胶，与 RU 价差看标胶强弱", "橡胶"),
    ("BR.SHF", "丁二烯橡胶", "轮胎", 2, 3, "跟随丁二烯与油价，对冲天胶成本", "橡胶"),
    # ---------------- 农产品 / 软商品 ----------------
    ("A.DCE", "豆一", "种植业", 3, 2, "国产大豆价格决定豆农与豆企利润", "农产品"),
    ("B.DCE", "豆二", "种植业", 2, 2, "进口大豆成本，压榨利润的另一端", "农产品"),
    ("C.DCE", "玉米", "种植业", 3, 2, "能量饲料主体，与深加工利润反向", "农产品"),
    ("RR.DCE", "粳米", "种植业", 2, 3, "反映稻谷最低收购价，波动小", "农产品"),
    ("M.DCE", "豆粕", "饲料", 3, 2, "饲料最大成本项：涨则压养殖利润", "农产品"),
    ("M.DCE", "豆粕", "养殖业", 2, 2, "饲料涨价压养殖利润（反向）", "农产品", _NEG),
    ("RM.ZCE", "菜粕", "饲料", 3, 2, "与豆粕互为替代，价差定饲料配方", "农产品"),
    ("CS.DCE", "玉米淀粉", "农产品加工", 2, 3, "淀粉-玉米价差即深加工利润", "农产品"),
    ("Y.DCE", "豆油", "农产品加工", 3, 2, "压榨主要利润来源，与棕油替代", "农产品"),
    ("OI.ZCE", "菜油", "农产品加工", 3, 2, "受进口菜籽与收储政策影响", "农产品"),
    ("P.DCE", "棕榈油", "农产品加工", 3, 2, "油脂定价锚，产地政策驱动", "农产品"),
    ("S", "CBOT大豆", "种植业", 3, 2, "全球定价中心，隔夜定豆粕豆油开盘", "农产品", _POS, _FOR),
    ("W", "CBOT小麦", "种植业", 2, 2, "口粮定价基准，与玉米存在饲用替代", "农产品", _POS, _FOR),
    ("C", "CBOT玉米", "种植业", 3, 2, "与国内价差决定进口利润与替代需求", "农产品", _POS, _FOR),
    ("SM", "CBOT豆粕", "饲料", 2, 2, "直接映射国内豆粕的成本端", "农产品", _POS, _FOR),
    ("BO", "CBOT豆油", "农产品加工", 2, 2, "叠加生柴政策，全球油脂定价锚", "农产品", _POS, _FOR),
    ("SR.ZCE", "白糖", "农林牧渔", 2, 2, "糖价决定糖料种植与制糖利润", "软商品"),
    ("CF.ZCE", "棉花", "农林牧渔", 2, 2, "棉价决定棉农与轧花厂利润", "软商品"),
    ("CY.ZCE", "棉纱", "纺织服饰", 2, 3, "纱价-棉价价差即纺企利润", "软商品"),
    ("AP.ZCE", "苹果", "农业种植", 2, 2, "减产/库存周期驱动，勿与苹果产业链混", "软商品"),
    ("CJ.ZCE", "红枣", "农业种植", 2, 2, "小品种大波动，减产年弹性极大", "软商品"),
    ("PK.ZCE", "花生", "农业种植", 2, 2, "影响花生油与食品加工成本", "软商品"),
    ("RS.ZCE", "菜籽", "农业种植", 2, 3, "收购价决定小榨作坊与菜油成本", "软商品"),
    ("CT", "ICE棉花", "纺织服饰", 2, 2, "外棉价是进口成本与内外价差另一端", "软商品", _POS, _FOR),
    ("SB", "ICE白糖", "农林牧渔", 2, 2, "原糖定配额外进口成本与内外价差", "软商品", _POS, _FOR),
    # ---------------- 养殖 / 浆纸 / 航运 ----------------
    ("LH.DCE", "生猪", "养殖业", 3, 3, "猪价决定养殖利润，产能周期定幅度", "养殖"),
    ("LH.DCE", "生猪", "农产品加工", 2, 3, "猪价涨→屠宰与肉制品受压（反向）", "养殖", _NEG),
    ("JD.DCE", "鸡蛋", "养殖业", 2, 3, "蛋价与饲料成本决定蛋鸡养殖利润", "养殖"),
    ("SP.SHF", "纸浆", "造纸", 3, 3, "浆价占纸企成本六成以上，涨则压毛利", "浆纸"),
    ("OP.SHF", "双胶纸", "造纸", 2, 3, "纸价-浆价价差即纸企利润", "浆纸"),
    ("EC.INE", "欧线集运", "航运", 4, 2, "运价即集运收入，绕行事件放大弹性", "航运"),
    # ---------------- 宏观流动性（辅助：只作背景，不参与板块评分） ----------------
    ("IXIC", "纳斯达克指数", "科技板块情绪", 1, 1, "隔夜纳指影响科技偏好", "宏观", _AUX, _FOR),
    ("HSI", "恒生指数", "宏观流动性", 1, 1, "港股是外资定价中国资产的窗口", "宏观", _AUX, _FOR),
    ("DJI", "道琼斯指数", "宏观流动性", 1, 1, "美股情绪影响北向偏好", "宏观", _AUX, _FOR),
    ("SPX", "标普500", "宏观流动性", 1, 1, "全球风险资产定价基准，影响开盘", "宏观", _AUX, _FOR),
    ("N225", "日经225", "宏观流动性", 1, 1, "亚太早盘先于 A 股，反映情绪", "宏观", _AUX, _FOR),
    ("GDAXI", "德国DAX", "宏观流动性", 1, 2, "欧洲风险偏好影响大宗出口", "宏观", _AUX, _FOR),
    ("USDCNH.FXCM", "离岸人民币", "宏观流动性", 1, 1, "人民币贬值→外资流出", "宏观", _AUX, _NON),
    ("DX", "美元指数", "宏观流动性", 1, 1, "美元走强→资金流出、风险偏好受压", "宏观", _AUX, _NON),
    ("USDJPY.FXCM", "美元日元", "宏观流动性", 1, 2, "日元套息的风向标", "宏观", _AUX, _NON),
    ("EURUSD.FXCM", "欧元美元", "宏观流动性", 1, 2, "美元弱→新兴市场受益", "宏观", _AUX, _NON),
    ("USDHKD.FXCM", "美元港元", "宏观流动性", 1, 2, "港元弱→资金流出香港", "宏观", _AUX, _NON),
    ("ZN", "十年美债主连", "宏观流动性", 1, 2, "美债价涨=收益率降=流动性宽松", "宏观", _AUX, _NON),
    ("513100.SH", "纳指100ETF", "科技板块情绪", 1, 2, "场内溢价反映科技情绪", "宏观", _AUX, _NON),
)


def _build(row: Sequence[Any], index: int) -> FutureMapping | None:
    """把一行元组转成 `FutureMapping`；字段非法时记缺口并返回 None。

    刻意**不抛错**：人工维护的静态表写错一行，应该只是少一条映射，而不是让整个期货子视图
    起不来 —— 与 `config.py` 对 YAML 的处理口径一致（缺口进 `TABLE_GAPS`）。
    """
    code = str(row[0] or "").strip()
    name = str(row[1] or "").strip()
    board = str(row[2] or "").strip()
    if not (code and name and board):
        TABLE_GAPS.append(f"第 {index} 行缺少 code/name/board：{row!r}")
        return None

    try:
        strength = int(row[3])
        lead_days = int(row[4])
    except (IndexError, TypeError, ValueError):
        TABLE_GAPS.append(f"{code} 的 strength/lead_days 不是整数：{row!r}")
        return None
    if not 1 <= strength <= 5:
        TABLE_GAPS.append(f"{code} 的 strength={strength} 超出 1-5，已按边界截断")
        strength = min(5, max(1, strength))

    logic = str(row[5] or "") if len(row) > 5 else ""
    chain = str(row[6] or "").strip() if len(row) > 6 else ""
    direction = str(row[7]).strip() if len(row) > 7 and row[7] else _POS
    kind = row[8] if len(row) > 8 and isinstance(row[8], FutureKind) else _DOM
    if direction not in (_POS, _NEG, _AUX):
        TABLE_GAPS.append(f"{code} 的 direction={direction!r} 不合法，已回落 positive")
        direction = _POS

    return FutureMapping(future_code=code, future_name=name, board_name=board,
                         direction=direction, strength=strength,
                         lead_days=lead_days, logic=logic, chain=chain, kind=kind)


def _build_all() -> list[FutureMapping]:
    """构建全表并按 (code, board) 去重（重复行保留第一条并记缺口）。"""
    built: list[FutureMapping] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(_RAW, start=1):
        mapping = _build(row, index)
        if mapping is None:
            continue
        key = (mapping.future_code, mapping.board_name)
        if key in seen:
            TABLE_GAPS.append(f"{mapping.future_code}→{mapping.board_name} 重复，已忽略")
            continue
        seen.add(key)
        built.append(mapping)
    return built


_CACHE: list[FutureMapping] = []


# ==================================================================
# 公开接口
# ==================================================================


def load_mappings() -> list[FutureMapping]:
    """返回映射表全量（进程内缓存；**纯内存**，不读盘、不发网络请求）。

    表是静态数据，缓存只是省掉每次重建 100 多个 dataclass 的开销。返回新 list
    （元素仍是缓存对象），调用方**不要就地修改**元素 —— 带校准值请用 `calibrate()`。
    """
    if not _CACHE:
        _CACHE.extend(_build_all())
    return list(_CACHE)


def mappings_by_chain() -> dict[str, list[FutureMapping]]:
    """按产业链分组（"产业链级异动"的输入：同链 N 个品种同时异动；键按 `chains()` 排序）。"""
    grouped: dict[str, list[FutureMapping]] = {chain: [] for chain in chains()}
    for mapping in load_mappings():
        grouped.setdefault(mapping.chain or "未分类", []).append(mapping)
    return grouped


def chains() -> list[str]:
    """全部产业链名（去重、排序）。"""
    return sorted({m.chain for m in load_mappings() if m.chain})


def boards_for(future_code: str) -> list[str]:
    """某个期货代码对应的目标板块名（按表内顺序去重；未知代码返回空表）。"""
    wanted = str(future_code or "").strip().upper()
    if not wanted:
        return []
    return list(dict.fromkeys(m.board_name for m in load_mappings()
                              if m.future_code.upper() == wanted))


def futures_for_board(board_name: str) -> list[FutureMapping]:
    """反向查询：板块名 → 相关映射（**双向包含匹配**）。

    双向的理由：调用方手里的板块名来自两套口径 —— 申万一级是"有色金属"，同花顺概念
    是"白银概念"，前台可能只给"白银"两个字。双向包含让"白银"↔"白银概念"都能命中，
    调用方不必先知道表里存的是哪个。空串直接返回空表：`"" in 任意名` 恒真。
    """
    query = str(board_name or "").strip().lower()
    if not query:
        return []
    return [m for m in load_mappings()
            if query in m.board_name.lower() or m.board_name.lower() in query]


def effective_strength(mapping: FutureMapping) -> float:
    """实际使用的强度：有校准值用校准值，否则回落到人工设定的 `strength`。"""
    if mapping.calibrated_strength is None:
        return float(mapping.strength)
    return float(mapping.calibrated_strength)


# ==================================================================
# 季度校准（需求 5.3）
# ==================================================================


def _as_float_series(value: Any) -> pd.Series | None:
    """尽量把入参转成"已去空、已转 float"的序列；不可用返回 None。"""
    if value is None:
        return None
    series = value if isinstance(value, pd.Series) else pd.Series(value)
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    return numeric.astype(float) if not numeric.empty else None


def _pair(left: pd.Series, right: pd.Series) -> pd.DataFrame | None:
    """把两条收益率序列对齐成两列。

    日期索引（取数/回测返回的都是日期索引）走内连接 —— 期股两市休市日不同，只有真正
    同一天的收益才能配成一对。裸数组没有日期可用，则**按最新一根右对齐**：更常见的
    错误是左对齐，那会把 2026-09 的期货收益和 2026-03 的板块收益配成一对。
    """
    if isinstance(left.index, pd.RangeIndex) or isinstance(right.index, pd.RangeIndex):
        size = min(len(left), len(right))
        if size <= 0:
            return None
        return pd.DataFrame({"future": left.iloc[-size:].to_numpy(),
                             "board": right.iloc[-size:].to_numpy()})
    joined = pd.concat([left.rename("future"), right.rename("board")],
                       axis=1, join="inner").dropna()
    return joined if not joined.empty else None


def _window_corr(future_returns: Any, board_returns: Any, window_days: int,
                 min_obs: int) -> float | None:
    """过去 `window_days` 个观测的 Pearson 相关系数；样本不足/数据异常返回 None。

    返回 None（而不是 0.0）是刻意的：0 表示"确实不相关"，None 表示"没算出来"。把后者
    当 0 会系统性压低这条映射的强度，与 `models.py` "缺数据不参与加权"的口径一致。
    """
    try:
        left = _as_float_series(future_returns)
        right = _as_float_series(board_returns)
        if left is None or right is None:
            return None
        pair = _pair(left, right)
        if pair is None:
            return None
        window = pair.tail(window_days)
        if len(window) < min_obs:
            return None
        flat = min(float(window["future"].std(ddof=0)), float(window["board"].std(ddof=0)))
        if flat == 0.0:  # 停牌/停更的常数序列没有相关性可言
            return None
        return float(window["future"].corr(window["board"]))
    except Exception as exc:  # noqa: BLE001 校准是附加信息，不该中断主流程
        logger.debug("滚动相关性计算失败：%s", brief(exc, BRIEF_TIGHT))
        return None


def _lookup_board(boards: Mapping[str, Any], name: str) -> Any | None:
    """在 `{板块名: 序列}` 里找板块：先精确命中，再退化为包含匹配。"""
    if not name:
        return None
    direct = boards.get(name)
    if direct is not None:
        return direct
    for key, value in boards.items():
        text = str(key)
        if name in text or text in name:
            return value
    return None


def calibrate(mappings: Iterable[FutureMapping], *,
              board_returns: Mapping[str, Any] | None = None,
              future_returns: Mapping[str, Any] | None = None,
              window_days: int = RECALIBRATION_WINDOW_DAYS) -> list[FutureMapping]:
    """季度校准：用滚动相关系数写 `calibrated_strength`，**不动** `strength`。

    - `board_returns`：`{板块名: pd.Series}`，`future_returns`：`{期货代码: pd.Series}`，
      两者都应是**收益率**序列（喂价格序列会得到虚高的相关性，口径由调用方保证）。
    - 相关系数取最近 `window_days` 个观测（需求 5.3 的 250 个交易日），再换算到 1-5 星：
      `1 + 4 * min(1, |corr| / 0.8)`。取绝对值是因为方向已由 `direction` 表达，
      负相关同样是"强传导"。
    - **不覆盖人工 `strength`**，只写 `calibrated_strength` / `calibrated_at`（见模块
      docstring：两列回答不同问题，静默改写人工口径不可接受）。
    - 任何一条映射缺数据、序列对不上、算不出相关性，都只是**这一条**没有校准值（保持
      None），函数不抛错 —— 季度校准挂在自动任务上，不该因为一个新品种上市不满 250 天
      就整批失败。
    - 返回值是**新对象**（`dataclasses.replace`），不原地改入参：`load_mappings()` 的
      对象是进程级共享缓存，原地写入会让下次取表带上上次的校准值。
    """
    boards = board_returns or {}
    futures = future_returns or {}
    window = int(window_days) if int(window_days) > 1 else RECALIBRATION_WINDOW_DAYS
    min_obs = max(_MIN_OBS_FLOOR, window // 2)
    stamp = dt.date.today().strftime("%Y%m%d")

    calibrated: list[FutureMapping] = []
    for mapping in mappings:
        corr = _window_corr(futures.get(mapping.future_code),
                            _lookup_board(boards, mapping.board_name),
                            window, min_obs)
        if corr is None or not math.isfinite(corr):
            calibrated.append(mapping)
            continue
        score = round(1.0 + 4.0 * min(1.0, abs(corr) / _CORR_FULL_SCALE), 2)
        calibrated.append(dataclasses.replace(
            mapping, calibrated_strength=score, calibrated_at=stamp))
    return calibrated


__all__ = ["RECALIBRATION_INTERVAL_DAYS", "RECALIBRATION_WINDOW_DAYS", "TABLE_GAPS",
           "boards_for", "calibrate", "chains", "effective_strength",
           "futures_for_board", "load_mappings", "mappings_by_chain"]
