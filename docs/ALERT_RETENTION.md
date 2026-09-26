# 事件告警保留三天并自动删除

> 用户口径（2026-09-26）：
> 「事件告警的信息最多保留三天，超过3天的信息自动溢出删除。」

---

## 一、改动前的实际行为（比预想的差）

走查发现**两个机制都不满足需求**：

| 机制 | 改动前 | 问题 |
|---|---|---|
| `_expire_due`（读路径懒过期） | 把到期告警 `status='expired'` | **只是让列表不显示 —— 行永远留在库里** |
| `retention_alert_days` | 默认 **365 天** | 一年才删，且 |

所以"最多保留三天"这一条**一条都没实现**：
- 可见性上：`alert_expire_days=7`，到期后 7 天才隐藏；
- 存储上：`fact_alerts` **只增不减**（懒过期不删行），
  而 `retention_alert_days=365` 意味着一年内的过期告警全在库里。

**这正是"保留策略静默失效"的典型形态** —— 不清理不报错，
只会某天发现库很大。

---

## 二、一个中途纠正的设计错误（值得记下来）

我最初的实现是**在 `run_retention` 里新增一段**
`prune_alerts_before()` 调用。但走查后发现**已经有一条更完备的删除路径**：

```python
# retention_passes.py —— 已存在（默认开启）
RetentionPass(
    name="fact_alerts", time_column="expire_time", unit="days",
    setting="retention_alert_days",
    condition="status = 'expired'",
    cascades=(
        Cascade(table="user_alert_read", child_key="alert_id",
                parent_key="alert_id"),      # ← 新路径没有这条！
    ),
),
```

它带一个**级联**删 `user_alert_read`（告警的"已读"记录）。
而我的 `prune_alerts_before` **没有**这条级联 —— 两套并存时谁先跑谁生效：

- 我的路径先跑 → 留下引用不存在告警的 `user_alert_read` **孤儿行**
  （"已读"记录指向一条被删掉的告警），表现是**数据不自洽**
- 而且同一件事有两条实现，将来必然分叉

**所以正确的落法是改保留期数值，而不是新开一条删除路径。**
`prune_alerts_before` 我保留了 —— 作为**显式 API**（供运维/脚本按任意
截止日清理，带完整测试），但**不参与每日作业**。

---

## 三、最终实现（两个字段，必须一起改）

```python
# src/core/config.py
alert_expire_days: int = 3        # 7 → 3
retention_alert_days: int = 3     # 365 → 3
```

| 字段 | 作用 | 路径 |
|---|---|---|
| `alert_expire_days = 3` | `expire_time = trigger_time + 3天` | 读路径 `_expire_due` 置 `expired` → **列表不再显示** |
| `retention_alert_days = 3` | 按 `expire_time` 真正 `DELETE` + 级联 `user_alert_read` | 每日 03:30 `data_retention_daily` |

**两者取同一个值（3）是刻意的**：到期即删除，不留"已过期但还占着库"的中间态。

⚠️ **危险组合是"删除期 < 过期期"**：那会让一条**还没到期、列表上还该显示**
的告警被物理删掉 —— 用户看到列表莫名少东西，而日志只会说"删除了 N 行"，
完全联想不到配置。所以加了一条测试守这个不变式。

### 时序

```
T+0      告警产生，expire_time = T+3天
T+3天    读路径 _expire_due 置 expired  → 列表不再显示（行还在）
T+3天    （当日 03:30）data_retention_daily 真正 DELETE + 级联
```

---

## 四、新增的显式清理 API

`EventRepository.prune_alerts_before(cutoff, tenant_id=None, delete_orphan_events=True)`
→ `{"alerts": n, "events": m}`

供运维/脚本按任意截止日清理（不依赖 `expire_time`，用 `trigger_time` 判定）。

设计要点（都写进了 docstring）：

1. **空截止日抛 `ValueError`**，绝不"删光全表" —— 静默删光的话用户看到
   "告警都没了"，而任何日志都不会说为什么。默认实现抛
   `NotImplementedError` 而**不是**静默返回 `{"alerts": 0}` —— 后者会让
   保留作业报告"清理完成"而数据一条没少。
2. **孤儿事件一起删**：`fact_events` 的行只被告警引用，只删告警会留下孤儿，
   而 `list_unanalyzed_events`（按 `analyzed=0` 取候选）会把它们当
   "未评估的积压"**反复捞出来** —— "清干净了"只是看起来干净。
3. **事件的"新旧"按事件自己的日期判**，取 `publish_time` 与 `created_at`
   中**较早**的那个日期前缀。踩过的两个坑：
   - `created_at` 是**入库时刻**（`'YYYY-MM-DD HH:MM:SS'`，无 `T`），
     而 `cutoff_text` 是 ISO 带 `T` —— **字符串直接比较会错**，
     结果是"孤儿事件一条都删不掉"且**不报任何错**；
   - 更根本的是 `created_at` **不该**用来判"事件有多旧"：补采一条两个月前的
     新闻时 `created_at` 是今天，用入库时间判它会被当成"新鲜事件"永远留着。
   取较早是**保守**方向 —— 只有按任何口径都算旧的事件才会被删；
   两者都取不到时**保留**（宁可留一行，也不误删）。
4. **未超期的孤儿事件不删**：采集与"生成告警"不是同一时刻
   （`list_unanalyzed_events` 是一轮轮跑的）。删掉 1 天前、还没产出告警的
   事件等于**丢掉一条还没被评估的线索**。

---

## 五、验证

新增 `tests/unit/test_alert_retention.py`（10 个用例）：

```
test_prunes_alerts_older_than_cutoff              超期必须真删（不是只置状态）
test_keeps_alerts_within_retention                保留期内一条不能少
test_orphan_events_are_removed_with_their_alerts  超期孤儿事件一起删
test_recent_orphan_event_is_kept                  未超期孤儿保留（可能还没被分析）
test_shared_event_is_not_deleted                  仍被引用的共享事件不删
test_empty_cutoff_raises_instead_of_wiping_table  空截止日报错而非删光
test_prune_is_idempotent                          重复跑不报错、不误删
test_default_retention_is_three_days              两个字段都必须是 3
test_retention_service_reports_alert_cutoff       截止日必须报出来
test_retention_days_align_with_expire_days        删除期 ≥ 过期期（防误删未到期）
```

结果：**10 passed**；`ruff` 全绿。

同批跑保留/告警相关既有测试：**58 passed, 1 failed** ——
那 1 个失败是 `test_alert_models.py::test_settings_alert_defaults`，
断言 `alert_confidence_min == 0.70` 而实际 `0.6`（环境变量覆盖），
**是改造前就存在的失败，与本次改动无关**（该用例根本没断言
`alert_expire_days`）。

---

## 六、需要你确认的两件事

1. **现有存量数据**：改动**不会**自动清理历史告警 ——
   下次 `data_retention_daily`（每日 03:30）才会按新保留期删。
   想立刻清理可以手动跑一次（会级联删 `user_alert_read`）：

   ```bash
   python -c "
   import asyncio
   from src.infrastructure.retention_service import run_retention
   print(asyncio.run(run_retention()))
   "
   ```

2. **`retention_alert_days` 也可以只走环境变量**：如果 3 天只是当前策略、
   以后想调，不用改代码 —— 设 `RETENTION_ALERT_DAYS=7` 即可。
   但**记得同时**设 `ALERT_EXPIRE_DAYS`，保持"删除期 ≥ 过期期"。
