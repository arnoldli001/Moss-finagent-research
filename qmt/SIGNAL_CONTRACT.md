# 信号契约 v1 —— 本地选股端 ⇄ QMT 执行端

> 这是两边**唯一**的耦合面。改这里必须同时改 `signal_server.py` 与 `qmt_executor.py`，
> 并同步提升 `schema` 版本号。

## 0. 为什么要拆成两端

QMT 的 `xtdata` **没有**「涨停原因/题材」数据，导致打分模型里
**题材热度(12) + 题材龙头(3) + 封流比(6) + 昨日质量的一半(2.5) = 23.5/100 权重**算不出来。
把它硬塞进 QMT 只能靠归一化降级，分数分布会变、门槛要重标。

拆开之后各司其职：

| | 本地选股端 | QMT 执行端 |
|---|---|---|
| 数据 | warehouse.db + eltdx（题材、竞价秒级序列、涨停原因） | 券商行情 + 交易柜台 |
| 职责 | **完整 12 维打分**、选股、仓位意图、规则①判定 | **下单/撤单/成交回报**、价格类卖出规则 ②-⑦ |
| 优势 | 数据全、无延迟压力（9:25 后 ~6s 完成） | 离柜台最近、行情最准、断网也能自保 |
| 不做什么 | 不碰账户、不下单 | **不做选股**（没数据，做了也是错的） |

**分工原则**：**买入决策 100% 在本地**（QMT 没有选股能力，收不到信号就绝不开仓）；
**价格类卖出决策 100% 在 QMT**（14:56 那一刻它离行情最近，且不依赖传输）。

---

## 1. 传输方式

### 1.1 文件投递（默认，同机部署）
```
signals/
  buy_20260919.json          本地 → QMT   买入计划（09:25 写，09:31 过期）
  sellalert_20260919.json    本地 → QMT   规则①清仓名单（09:26 写，15:00 过期）
  fill_20260919.json         QMT → 本地   成交回报（每次执行后覆盖写）
  heartbeat.json             本地 → QMT   心跳（每 5s 一次，QMT 据此判断是否降级）
```

**必须原子写**：先写 `xxx.json.tmp`，再 `os.replace()` 改名。否则 QMT 可能读到半截 JSON。

### 1.2 HTTP（跨机部署，可选）
本地 `signal_server.py --http-port 8765` 暴露只读接口：
```
GET /heartbeat          → heartbeat.json
GET /buy?date=YYYYMMDD  → buy 计划
GET /sellalert?date=... → sellalert
POST /fill             → QMT 回传成交回报
```
QMT 端 `qmt_executor.py --signal-url http://<本地IP>:8765` 切换为 HTTP 拉取。
**同一个 `plan_id` 只能被执行一次**，HTTP 模式下这条约束由 QMT 端的状态文件保证。

---

## 2. 通用信封（所有信号共用）

```jsonc
{
  "schema": "auction_v3.signal/1",     // 契约版本，QMT 端不匹配则拒收
  "plan_id": "20260919-buy-7f3a2c",    // 全局唯一，QMT 用它做幂等
  "trade_date": "20260919",
  "phase": "buy",                      // buy | sell_alert
  "generated_at": "2026-09-19T09:25:11+08:00",
  "expire_at":   "2026-09-19T09:31:00+08:00",  // 过期一律不执行
  "source": {
    "host": "DESKTOP-01",
    "rules_hash": "6f4be9710912",      // 规则指纹，变更可追溯
    "mode": "full",                    // full=12维完整 | degraded=缺维度
    "missing_dims": []                 // degraded 时列出缺哪些
  },
  "market": {                          // 供 QMT 兜底卖出规则① 使用
    "stage": "发酵期",
    "temperature": 68,
    "max_streak": 5,
    "broken_rate": 0.21,
    "allowed": true                    // false = 周期闸门关闭，QMT 也不许开仓
  },
  "guards": {                          // QMT 必须逐条校验，任一不满足则整单拒收
    "max_total_amount": 500000,        // 本次计划买入总额上限（元）
    "max_orders": 5,                   // 本次计划最多几笔
    "min_cash_reserve": 0              // 执行后至少保留的现金
  },
  "positions": [ /* 见 §3 / §4 */ ],
  "notes": ["..."]                     // 人类可读的说明，QMT 只记录不解析
}
```

---

## 3. `phase = "buy"` —— 买入计划

```jsonc
"positions": [
  {
    "code": "600000.SH",          // 统一 .SH/.SZ 后缀
    "name": "浦发银行",
    "action": "BUY",
    "weight": 0.36,               // 目标仓位 = 权益 × weight（已含抢筹 ×1.2）
    "add_ratio": 0.30,            // 若已持有该股 → 改用 已有市值 × add_ratio
    "max_weight": 0.35,           // 执行后该股总市值 ≤ 权益 × max_weight
    "ref_price": 10.50,           // 9:25 撮合价，QMT 据此算限价
    "slip_pct": 0.01,             // 限价 = min(涨停价, ref_price × (1+slip_pct))
    "score": 71.3,
    "reason": "竞价入选 71.3分；题材「光伏」热度0.72 龙头",
    "tags": ["抢筹"],             // 抢筹 | 大量抢筹 | 抢跑
    "extra": {                    // 仅用于复盘，QMT 不解析
      "open_gap_pct": 3.42,
      "takeover_score": 68.5,
      "auction_volume_ratio": 4.8,
      "theme_name": "光伏"
    }
  }
]
```

**仓位由谁定**：本地给**比例**（`weight`），QMT 用**自己的实时权益**换算成金额与股数。
这样本地不需要知道账户余额，也不会因为余额变动导致仓位失真。
`add_ratio` 与 `max_weight` 由 QMT 在**知道实时持仓**的前提下套用。

**顺序**：QMT 按数组顺序执行（本地已按分数降序排好）。

---

## 4. `phase = "sell_alert"` —— 规则①清仓名单

只有**需要题材/竞价序列**的卖出规则①走这条通道：

> ① 市场最高连板 ≥7 **且持仓盈利** **且当日竞价带「抢跑」标签** → 全部清仓

「抢跑」标签必须在 **9:25 定格时**算（14:56 已拿不到竞价序列），所以本地早上算好、
全天有效，QMT 在 14:56 判定时取用。

```jsonc
"positions": [
  {
    "code": "600000.SH",
    "action": "SELL_ALL",          // 只支持 SELL_ALL
    "reason": "最高连板≥7 且盈利且竞价抢跑 清仓",
    "tags": ["抢跑"],
    "jump_gap": 0.9821,            // 复盘用
    "auction_volume_ratio": 14.2
  }
]
```

**优先级**：QMT 收到该名单后，规则① 的判定顺序**高于**所有价格类规则 ②-⑦
（与回测一致）。名单里没有的票，QMT **不会**自己判规则①（它没有抢跑数据）。

---

## 5. `fill_*.json` —— QMT 回执（本地据此对账）

```jsonc
{
  "schema": "auction_v3.fill/1",
  "trade_date": "20260919",
  "plan_id": "20260919-buy-7f3a2c",
  "executed_at": "2026-09-19T09:30:12+08:00",
  "account": "****0365",
  "equity": 1012345.60,
  "cash": 412300.00,
  "market_value": 600045.60,
  "orders": [
    {"code": "600000.SH", "side": "BUY", "plan_status": "filled",
     "planned_amount": 360000, "order_id": 123456,
     "volume": 34000, "price": 10.58, "amount": 359720, "fee": 35.97,
     "reason": "竞价入选", "error": ""}
  ],
  "positions": [
    {"code": "600000.SH", "volume": 34000, "can_use": 0,
     "avg_price": 10.58, "market_value": 359720, "buy_day": "20260919"}
  ],
  "rejected": [],                  // 被 guards 拒收的整单说明
  "warnings": []
}
```

`plan_status` 取值：`filled`（全部成交）/ `partial`（部分成交）/ `pending`（未成交）/
`rejected`（柜台拒单）/ `skipped`（QMT 主动跳过，如资金不足、已达上限）。

---

## 6. QMT 执行端的强制校验（缺一不可）

| 校验 | 不通过时 |
|---|---|
| `schema` 精确匹配 `auction_v3.signal/1` | 拒收整单并告警 |
| `trade_date` == 今天 | 拒收（防隔夜陈旧信号） |
| `now <= expire_at` | 拒收（防过期信号） |
| `plan_id` 未执行过 | 拒收（**幂等**，重启不重复下单） |
| `phase` 与当前时段匹配 | 拒收 |
| `len(positions) <= guards.max_orders` | 拒收 |
| `Σ(金额) <= guards.max_total_amount` | 拒收 |
| `market.allowed == true` | 不下买单（但仍执行卖出） |
| 每只 `weight <= max_weight` | 该只跳过，其余照常 |

**降级模式（fail-safe）**：QMT 记录 `heartbeat.json` 的最后更新时间。
超过 `SIGNAL_TIMEOUT_SEC`（默认 180s）未更新时：
- **停止一切开仓**（本地不在线 → 没有选股依据）；
- **保留价格类卖出规则 ②-⑦ 全部生效**（这些只依赖本地行情，不依赖传输）；
- 规则① 降级为不判定（没有抢跑数据，宁可不做）；
- 在 `fill_*.json` 的 `warnings` 里明确标注本次为降级运行。

这条设计的意义：**本地电脑关机、断网、崩溃，持仓依然有止损保护。**

---

## 7. 一天的时间线

```
09:10  本地  signal_server.py --phase preheat      预热（昨日涨停/连板/题材，~180s）
09:25  本地  signal_server.py --phase buy          竞价定格 → 完整12维打分 → 写 buy 计划
09:25  QMT   qmt_executor.py --phase buy           读计划 → 校验 → 09:30 开盘成交
09:26  本地  signal_server.py --phase sellalert    算持仓股「抢跑」标签 → 写 sell_alert
14:52  QMT   qmt_executor.py --phase sell          价格类规则②-⑦ + 规则①（读 sell_alert）
14:57  QMT   委托参与收盘集合竞价
15:00  QMT   写 fill 回执
```

> **为什么买入不要求本地在 9:25 卡点**：QMT 在 9:25-9:30 报的单本来就进 9:30 连续竞价，
> 本地 9:25:05 起跑、6s 出结果、9:25:20 前送达，时间充裕。

---

## 8. 版本演进

`schema` 字段做**精确匹配**而非前缀匹配 —— 宁可拒收也不要半懂不懂地执行。
新增字段一律**可选**，QMT 端对未知字段一律忽略；删除或改语义必须升版本号。
