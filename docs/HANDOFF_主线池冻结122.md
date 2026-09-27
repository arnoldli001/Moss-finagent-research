# 交接：把主线概念池冻结为「20 日胜率筛出的 122 个」

> 交接自 2026-09-27 会话。**新会话请先读本文，再动手。**
> 我（上一个会话）在这件事上**改口过五次**，所以本文把"已知的坑"单列一节。

---

## 一、目标（用户原话）

> 「主线板块所需计算的概念板块数量只有 122 个，不是 262 个，只是这 262 个里包含了这 122 个。」
>
> 「根据一个板块历史告警的 20 日胜率 > 45% 还是 40% 我忘了，筛选出来的，
> 你就把这当签的 122 个概念板块，**写死在主线挖掘的概念池里**，
> 后续只关注这 122 个，**不会再有变动**。」

即：**用 20 日胜率判据算出 122 个概念板块 → 冻结成主线概念池 → 后续不再动态变动。**

---

## 二、现成机制（不要新造轮子）

| 件 | 位置 | 说明 |
|---|---|---|
| 门槛常量 | `src/mainline/alert_returns.py:87` | `DEFAULT_MIN_WIN_RATE = 0.4` |
| 判据实现 | `src/mainline/theme_gate.py` | 门槛是**严格大于**；窗口 <3 个的"判不准"**留在池内** |
| 生成脚本 | `scripts/build_theme_exclusions.py` | `--threshold` / `--min-samples` |
| 现行台账 | `configs/mainline_theme_exclusions.yaml` | 头部写死「剔除 = 胜率 ≤ **40%** 且已走满窗口 ≥ 3 个」，34 条 |
| 池导入 | `src/mainline/datastore.py:1805` `import_crowding_pool()` | 现在的池 = 全表 − `sector_blacklist.yaml` |

⚠️ `scripts/build_theme_exclusions.py --help` **会崩**（argparse 的 `%` 格式化撞上中文全角括号），
别在它上面浪费时间；直接读代码或绕过 `--help`。

---

## 三、待做的四步

1. **定门槛**：跑一遍胜率，同时报 40% 和 45% 各筛出多少 —— **用户忘了是哪个**，
   用数据定，别问。
2. **冻结名单**：把筛出的集合写成一份新配置（建议 `configs/mainline_frozen_pool.yaml`），
   带 `decided: "2026-09-27"` 与生成脚本名，与其他"冻结清单"同风格。
3. **接线**：让 `import_crowding_pool` 以这份冻结名单为准
   （现在是「全表 − 黑名单」，要改成「冻结名单」）。
4. **重跑导入并验证** `ml_board` 落到 122。

---

## 四、两个必须确认的问题（动手前）

1. **122 是「262 里筛」还是「全板块筛」？**
   用户说「这 262 个里包含了这 122 个」→ 倾向前者（262 ∩ 胜率>门槛）。
   但若某板块不在 262 里却胜率很高，要不要进主线？**问一句再动。**
2. **胜率口径**：`configs/mainline_theme_exclusions.yaml` 用的是
   「**全档位**（strong/medium/weak）的信号」，而「回测收益展示」面板只看 strong/medium。
   两者结果不同 —— 确认用哪套。

---

## 五、当前状态（2026-09-27 收尾时）

| 项 | 值 |
|---|---|
| 拥挤度剔除清单 | **2255 条**（`configs/crowding_exclusions.yaml`，备份 `.bak-narrow`） |
| 拥挤度池 | **262**（`visible=1` 且 `is_concept=1`）✅ |
| 全量刷新池 | **262**（原 1846）✅ |
| 主线池 `ml_board` | **130**（从 138 收窄而来） |
| 主线应有 | **122** ← 本文要做的 |

**已做的改动**（`src/mainline/datastore.py`）：`import_crowding_pool` 在
`load_sector_blacklist()` 之外**再减去 `load_crowding_exclusions()`**，
保证「主线 ⊆ 拥挤度 262」。**这一步无论最终是 122 还是别的数都成立，建议保留。**

**回退方式**：去掉那两行（搜索 `crowding_excluded`）+ 重跑
`MainlineDataStore(load_config()).import_crowding_pool()` → 回到 138。

---

## 六、坑（本节最重要，别重复我的错）

1. **`sector_crowding_list` 会被主线读**（`mainline/datastore.py:1801`），
   但它用的是 **`sector_blacklist.yaml`**（第 1836 行写死），**不是**拥挤度那份
   —— 所以拥挤度拉黑**不会**踢出主线池（这条我查了两遍才敢确认）。
2. **拉黑必须是冻结快照，不能做成动态规则**。动态规则会死锁：
   新板块不在可见池 → 被判剔除 → `seed_list` 跳过 → 永远进不来。
   用户明确要求「后续手动新增的概念还是允许加入」。
3. **「恢复/新增」通路必须能撤销拉黑** —— `config.remove_crowding_exclusions()`
   已挂到 `POST /config_list` 与 `/config_list/restore` 上。
4. **`load_config()` 是 `lru_cache(maxsize=1)` 单例** —— 测试里改
   `config.window.*` 会**泄漏到后面的测试**，改完必须 `load_config.cache_clear()`。
5. **改代码后立刻跑 `ruff check --select F821`** —— 本会话我误删过一个变量定义
   （`workers`），93 条单测全绿但「一键刷新」整条路 NameError。
6. **`ml_board` 不在主库**，在 `data/mainline_cache.db`（`mainline_cache_path`）。
7. **周任务/幂等检查会假装成功** —— `compute_all_metrics` 同一周已有记录时，
   不加 `force=True` 直接跳过，看起来像"跑成功了"但口径没变。

---

## 七、如果要顺带做的（用户提过但未做）

| 项 | 说明 |
|---|---|
| 限流退避 | `ths_daily` 撞「500次/分钟」时退避重试（现为一次失败即放弃）。**根因未修** |
| 后端日志落盘 | 当前 uvicorn 的 stdout 没落到 `data/run/backend.log`，**重启后查不到日志** |
| 行业轮动启动预热 | 无报告时同步生成要 2 分 22 秒（`sector_rotation.py:141`） |

---

## 八、本会话产出、可供新会话参考

* skill：`skills/pipeline-redundancy-audit/SKILL.md`（链路审计 v2.0）
* 审计报告：`docs/LINK_AUDIT_REPORT_2026-09-27.md`
* 顺手修的：水位偏差校正、NaN 出口护栏、多因子库解耦、全量刷新台账
* 测试基线：`tests/unit/test_sector_crowding.py` + `test_safe_json_response.py`
  → **99 passed**
