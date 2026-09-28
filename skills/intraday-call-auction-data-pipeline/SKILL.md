---
name: intraday-call-auction-data-pipeline
description: |
  集合竞价（9:15-9:25）期间的实时数据链路缺口：A 股交易类系统最常见的两类洞。
  ① 后端 watchlist 刷新循环被 `is_trading_day()` 在竞价期误判为"非交易日"
    而整段跳过，导致 9:25-9:30 这 5 分钟面板上仍显示上一交易日收盘价。
  ② 前端分时图首根 bar 落在 9:30（连续竞价起点），没有 9:25 撮合价锚点，
    用户看不到"开盘定方向"那一帧。
  配套的 AI 编程反模式：把诊断当回答（用户问"为什么不刷新"时 AI 回"怎么看日志"
  而不回"怎么修"）、把两个症状当一个症状处理、提交时把别人预 staged 的文件一起
  带走。
triggers:
  - 集合竞价 / call auction / 09:25 / 9:25
  - 开盘前 5 分钟面板没数据
  - watchlist 没显示集合竞价涨幅
  - 开盘后分时图第一根是 9:30
  - 用户报障"行情没刷新 / 9:30 才出数"
  - "为什么 9:30 分才出分时"
  - 数据刷新起始时间不对
  - 竞价期 tick 不前进 / 市场时钟停在上一交易日
related:
  - skills/intraday-sell-points/SKILL.md           # 分时卖点（消费本 skill 产出的快照）
  - skills/auction-selection/SKILL.md              # 集合竞价选股（语料层 9:25 自动打分）
  - skills/ai-dev-loop-discipline/SKILL.md         # "被反复纠正"那一类共因
  - docs/SESSION_ROOTCAUSE_AND_FIXES_20260927.md   # 同类窗口陷阱的更早案例
source_project: Moss-FinAgent-Research
source_date: 2026-09-28
changelog: |
  v1.0  从 2026-09-28 用户报障抽出。两件事（后端闸门 / 前端锚点）
        根因不同，必须分开修；同时附 AI 编程三条教训。
---

# 集合竞价数据缺口：watchlist 不刷 9:25 + 分时图没 9:25 锚点

> **本 skill 不讲语料阈值**（那是 `auction-selection/` 的事），
> 也不讲竞价选股（那是 `auction-selection/` 的事）。本 skill 讲的是
> **竞价期间的实时数据链**：watchlist 的 `change_pct` 应该在 9:25:00
> 撮合后立刻更新、图上应该有一根 9:25 锚点 —— 现有系统两个都漏。

---

## §1 · 现象（用户报障原文）

> "今天已经开盘了 为什么行情里个股没刷新？"
>
> "为什么 9:30 分才出分时数据，9:25 集合竞价结束就应该显示在分时图上，
> 自选里竞价涨幅也应该刷新，所以数据刷新起始时间不对，没包含 9:25 分的"

| 表现层 | 看到什么 |
|---|---|
| watchlist 列 | 9:25 撮合后 5 分钟内仍显示 9/24 收盘价 + `change_pct=0.00%` |
| 分时图 | 第一根 bar 落在 `09:30:00`（连续竞价起点），9:15-9:30 全空 |
| 决策窗口 | "开盘定方向"那一帧没有视觉锚点，无法判断"开盘跳空 vs 跳高" |

---

## §2 · 根因（拆成两个独立的洞）

### 2.1 后端：`_watchlist_refresh_loop` 被双重闸门误杀

```python
# src/intraday/service.py 旧逻辑（2026-09-28 修复前）
allowed, reason = watchlist_refresh_window()    # ← 第一道闸门：09:15 起放行
if allowed and not _is_trading_day_for_refresh():  # ← 第二道闸门：节假日跳过
    # skip
```

**问题**：`is_trading_day()` 用**行情时钟 tick 的 timetag** 判定节假日
（`live_session_date()` 探的是指数快照的 31 位时戳）。
而 **A 股指数在集合竞价期没成交**（指数只有连续竞价，没有集合竞价），
所以指数 tick 不前进，`live_session_date()` 在 9:15-9:30 返回**昨天**，
`is_trading_day()` 因此把**真交易日**判成"非交易日"。

**为什么作者原来这么写**（`service.py:3010-3017` 的注释原文）：

> "竞价期 tick 还没推进，怕误杀正常交易日"

作者意识到了误杀的可能，但用**节假日判据**来拦，结果把"9:25 撮合价
看不到"和"节假日少跑一轮"两个问题压成一道闸门一起解决 —— 结果两边
都被它卡住。

### 2.2 前端：`IntradayChart` 没画 9:25 锚点

**`sessionMinute()` 把 9:30 之前全部映射为 `minute = 0`**：

```typescript
function sessionMinute(ts: string): number | null {
  ...
  if (minutes < morningOpen) return 0;   // 9:30 之前 = 0
  ...
}
```

分时数据源（腾讯 / 新浪 / 东财）**首条都是 9:30 起的连续竞价**，
9:15-9:25 那 10 根 call-auction bar 没有免费替代
（`AGENTS.md` 已登记："竞价链的 fallback 已改为 none，竞价逐秒序列
没有免费替代源"）。

但 `quote.open`（昨收 + 集合竞价撮合价）**9:25 之后就可用**（腾讯
`qt.gtimg.cn` 第 5 位字段），系统拿到了却没用。

---

## §3 · 修法（按"先查后改 + 最小化修复"纪律）

### 3.1 Plan A：后端闸门只在 MORNING_OPEN 之后启用

```python
# src/intraday/service.py:_watchlist_refresh_loop
while True:
    try:
        allowed, reason = watchlist_refresh_window()
        # ★ 9:15-9:30 集合竞价期间不查 is_trading_day()：
        #   行情时钟 tick 没推进会误判真交易日为非交易日。
        #   9:25 撮合后 watchlist 必须立刻看到开盘价 + 竞价涨幅。
        # 取舍：节假日工作日的 9:25-9:30 会多空跑一轮（48 × 3-5s），
        #   9:30 后市场时钟追上就恢复，跳过逻辑照常工作。
        now_moment = datetime.now()
        in_call_auction = (
            now_moment.hour * 60 + now_moment.minute) < MORNING_OPEN
        holiday_check = (
            allowed and not in_call_auction
            and not _is_trading_day_for_refresh())
        if holiday_check:
            self._refresh_state.update(
                last_run_at=now_moment.strftime("%H:%M:%S"),
                last_error="")
            logger.debug("非交易日跳过自选池整表重算（%s）", reason)
            await asyncio.sleep(_seconds_until_next_tick(interval))
            continue
```

**关键不变量**：

- ✅ 集合竞价撮合后（9:25）→ watchlist 立刻有开盘价
- ✅ 真交易日 9:30 后 → 节假日闸门恢复正常（市场时钟已推进）
- ✅ 节假日工作日 9:25-9:30 → 多空跑一轮（最坏 150s CPU，可接受）
- ❌ 千万别用 `is_holiday()`（本项目没有这个函数，硬编码节假日表
  必然过期，AGENTS.md 已强调）

### 3.2 Plan B：前端在分时线最左画 9:25 锚点

```tsx
{/* 集合竞价开盘标记（09:25 撮合价）。
    分时数据源首条都是 09:30 起的连续竞价（AGENTS.md 已登记"竞价链
    fallback=none"），但 quote.open 在 9:25 撮合后即可用。
    只在 view.start <= 0（视窗包含 9:30）时显示，缩放后自动让位。 */}
{(() => {
  const openPrice = quote?.open ?? null;
  const prevCloseVal = quote?.prev_close ?? null;
  if (openPrice === null || !Number.isFinite(openPrice) || openPrice <= 0)
    return null;
  if (prevCloseVal === null || prevCloseVal <= 0) return null;
  if (view.start > 0) return null;
  const yOpen = y(openPrice);
  const gapPct = ((openPrice / prevCloseVal) - 1) * 100;
  return (
    <g className="premarket-marker">
      <line x1={PAD.left} x2={PAD.left + 42}
            y1={yOpen} y2={yOpen}
            stroke="var(--low)" strokeWidth="1.2"
            strokeDasharray="4 2" opacity={0.85} />
      <polygon
        points={`${PAD.left - 6},${yOpen - 5} ${PAD.left},${yOpen} ${PAD.left - 6},${yOpen + 5}`}
        fill="var(--low)" />
      <text x={PAD.left + 5} y={yOpen - 6}
            fontSize="10" fontWeight="600" fill="var(--low)">
        09:25 开盘
      </text>
      <title>
        {`集合竞价撮合价 ${openPrice.toFixed(2)}（${gapPct >= 0 ? "+" : ""}${gapPct.toFixed(2)}% vs 昨收）`}
      </title>
    </g>
  );
})()}
```

**设计取舍**：

- **不动 `sessionMinute()`**：9:15-9:29 映射到 0 会让"分时线第一根
  落在 09:30"的语义保持不变。改它需要连带改 `view.start` 范围，
  240 分钟窗口被挤压得不偿失。
- **不用 `view.start = -5`**：用户缩放后会很难定位。锚点单独画、
  缩放后消失更清晰。
- **触发条件分四道**：open 存在 + 数值合法 + prev_close 合法 +
  `view.start <= 0`。任一不满足都安静 `return null`，不污染图。

---

## §4 · 数据链限制（**别在这里浪费时间**）

| 数据 | 9:15-9:25 是否有 | 来源 | 备注 |
|---|---|---|---|
| **指数分钟线** | ❌ | 腾讯 / 新浪 / 东财 / QMT | 指数没有集合竞价 |
| **个股分钟线** | ❌ | 同上 | 免费源均从 9:30 起返回 |
| **个股逐分时 trend** | ❌ | 同上 | 同上 |
| **个股快照 `quote.open`** | ✅ | 腾讯 `qt.gtimg.cn` 第 5 位 | 9:25:00 撮合后即可用 |
| **个股 `change_pct`** | ✅ | 腾讯 `qt.gtimg.cn` 第 32 位 | `(open - prev_close) / prev_close * 100` |
| **QMT tick 逐秒** | ✅ | `xtdata.get_full_tick` | 已在本项目关停（QMT 无权限） |

→ **9:25 那一帧的"开盘价 / 涨幅"是免费的**；**9:25 那一帧的"分钟线轨迹"是付费的**。
能做的：前者用 `quote.open` 补一个点（图上 9:25 锚点）；
做不到的：分钟线轨迹（除非 QMT 权限恢复）。

---

## §5 · 测试要点（防御用例必须能复现）

### 5.1 后端回归（`tests/unit/test_intraday_push_guard.py`）

```python
def test_refresh_loop_runs_during_call_auction_on_real_trading_day(
        monkeypatch, freeze_at) -> None:
    """★ 真交易日 09:25 集合竞价撮合后必须立刻刷新。"""
    freeze_at(2026, 9, 24, 9, 25)          # 钉在集合竞价撮合那一刻
    # 即便 is_trading_day() 仍判 False（市场时钟没推进），
    # 集合竞价窗口里整表重算也必须真跑 —— 这是这次修复的目的。
    monkeypatch.setattr("src.intraday.auto_select.is_trading_day",
                        lambda moment=None: False)
    # ... 跑循环 + 断言 computed 不为空


def test_refresh_loop_skips_recompute_on_holiday(
        monkeypatch, freeze_at) -> None:
    """★ 节假日闸门必须仍然拦下 —— 但要避开集合竞价窗口。"""
    freeze_at(2026, 9, 28, 10, 0)          # 周一 10:00，已开盘 + 非竞价期
    # ... 跑循环 + 断言 computed 为空
```

**两条用例必须同时存在**：第一条修的是"集合竞价期不该被误杀"，
第二条守的是"节假日不能空跑" —— **改了 Plan A 一定**同时验这两条。

### 5.2 前端验证

```bash
cd web && npx tsc --noEmit -p tsconfig.json    # 必须 0 错误（quote.open 类型是 number | null）
cd web && npx vite build                        # 必须成功
```

**类型层最容易踩**：`IntradayQuote.open` 是 `number | null`，必须
显式收 null 再喂给 SVG 坐标（不能靠 `Number.isFinite()` 漏过）。

---

## §6 · AI 编程反模式（本轮三条教训）

### 6.1 把诊断当回答

**症状**：用户问 "今天已经开盘了 为什么行情里个股没刷新？"，
AI 第一轮回复"按这个顺序排查：点强制刷新 / 看数据源健康 / 如果腾讯
全 403 等源恢复" —— **把诊断当回答交付了**。

**问题**：用户已经看到了 stale 数据（"2026-09-24 的完整分时"），
这不是"怎么排查"的问题，是"为什么 9:30 才出数 + 怎么修"的问题。
用户更想要的是 fix path，不是 debugging path。

**纪律**：诊断型回复**必须附带"如果你判断根因是 X，修复路径是 Y"**。
诊断和修复是同一件事的两面 —— **只给诊断等于把球踢回给用户**，
等于把"找到根因"的负担从 AI 移到了用户头上。

### 6.2 把两个症状当一个症状

**症状**：用户报"行情里个股没刷新" + "9:25 应该显示在分时图上"。
AI 第一轮回复**只在 watchlist / 刷新循环里找**（找到了 is_trading_day
那道闸门），**完全没提前端图上没 9:25 锚点**。

**问题**：这两个症状**根因不同**：
- watchlist 不刷新 → 后端闸门误杀
- 分时图没 9:25 → 前端没画

**纪律**：用户报的每一个症状**先列清单再合并归因**。同一句"为什么不
刷新"背后可能是 N 个独立 bug，**找到第一个就停** = 漏掉 N-1 个。
正确流程：

1. 把用户描述的所有表现列成可观察清单
3. 对每个清单项分别查根因
4. 找到多个根因时**分别修**而不是合并

### 6.3 提交时不验 staged 集

**症状**：执行 `git add my_3_files && git commit -F msg`，
commit 里多了 3 个**别人预 staged**的文件（`.gitignore`、
`src/api/routes/auction_select.py`、`src/api/routes/quant_select.py`）。

**问题**：`git commit` 不带路径时**默认 commit 所有 staged 内容**，
不是"我刚 add 的那些"。AI 把 "git add" 和 "git commit" 的语义
和"`git commit my_files`"混为一谈。

**纪律**：commit 前**必须**先 `git diff --staged --name-only` 验
一遍 staged 集；任何不在本次修复范围的文件都先 `git restore --staged`
踢出去。这条不是事后补的，是**写 commit message 之前**就要做。

---

## §7 · 检查清单（写新代码前 / 改完代码后各跑一遍）

### 写新代码前（场景判断）

- [ ] 用户报障**列清单**：把每一个观察到的表现单独写成一行
- [ ] 每个表现**分别查根因**：不要因为找到第一个就停
- [ ] 涉及"时间窗口"的代码：画出门的判定逻辑图，标注节假日 / 周末
      / 集合竞价 / 早盘 / 收盘后各自落入哪道门
- [ ] 数据源**是否真的能拿到**这段数据？免费源没有 9:15-9:25 分钟线
      是 AGENTS.md 已登记的事实，不要在这上面再次发明轮子

### 改完代码后

- [ ] **同时验正反两个用例**：Plan A 修了"集合竞价不该被拦"，
      必须再守"节假日不能空跑"
- [ ] 时间相关的修复**用 `freeze_at` 钉死时间**：不要靠"现在几点是
      几点"作为前提
- [ ] 前端改完跑 `npx tsc --noEmit` —— 类型层漏 `null` 是最常见的
      二次 bug 源头
- [ ] `git diff --staged --name-only` 验 staged 集再 commit

---

## §8 · 关联定位（哪些代码片段属于本类）

| 路径 | 角色 |
|---|---|
| `src/intraday/service.py:_watchlist_refresh_loop` | 刷新循环（Plan A 落地点） |
| `src/intraday/sources.py:_probe_tencent_session_date` | 市场时钟探针（误判源头） |
| `src/intraday/auto_select.py:is_trading_day` | 节假日判据（被 Plan A 改用时机） |
| `src/core/trading_session.py:WATCH_WINDOWS` | 窗口边界常量（`09:15-11:30` 已含竞价期） |
| `web/src/components/IntradayChart.tsx:sessionMinute` | 时间映射（Plan B 验证不动它的理由） |
| `web/src/components/IntradayChart.tsx` （分时线之后） | Plan B 落地点（premarket-marker `<g>`） |
| `tests/unit/test_intraday_push_guard.py` | 两条防御用例 |