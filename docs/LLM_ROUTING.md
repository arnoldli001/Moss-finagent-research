# LLM 路由与熔断

> 配置：`configs/models.yaml`（路由与模型定义）、`.env`（`DEEPSEEK_API_KEY`）
> 代码：`src/infrastructure/llm/`（gateway / providers / circuit_breaker / cache / audit）
> 日志：`data/audit/llm_audit.jsonl`（每次调用一条，含 `error` 字段）

## 一、路由表：每层的 fallback 必须换 provider

| 层级 | 主模型 | 备模型 | 用途 |
|---|---|---|---|
| `light` | `local_light` (ollama) | `deepseek-flash` | 数据清洗、格式化 |
| `medium` | `local_medium` (ollama) | `deepseek-flash` | 信息层（A05 去伪 / A06 提取） |
| `reasoning` | `deepseek-flash` | `local_medium` (ollama) | 宏观/中观/微观/财务风险 |
| `decision` | `deepseek-v4-pro` | `local_medium` (ollama) | 投研建议综合（A17） |

**不变式：每层的 primary 与 fallback 必须属于不同 provider。** 同一 provider 下
换模型名不算降级 —— 它们共用同一个 API 端点、同一份 key、同一个熔断器，
provider 整体不可用时主备会一起失败。

> 实测故障（2026-09-20）：`decision` 层原本是
> `deepseek-v4-pro → deepseek-flash`，两者同属 `provider=deepseek`。
> 熔断后主备一起被拒，前端直接报
> 「全部模型调用失败（链: deepseek-v4-pro→deepseek-flash）」
> —— 而本机 Ollama 明明健康却没人用。
> 回归测试：`tests/unit/test_llm_gateway.py::test_routing_fallbacks_are_cross_provider`。

## 二、熔断器三态

实现：`src/infrastructure/llm/circuit_breaker.py`，按 provider 隔离
（`CircuitBreakerRegistry`）。deepseek 的默认参数：

| 参数 | 值 | 含义 |
|---|---|---|
| `failure_threshold` | 3 | 窗口内失败达到 3 次即 OPEN |
| `failure_window_sec` | 60 | 失败计数窗口 |
| `recovery_cooldown_sec` | 30 | OPEN 保持 30s 后转 HALF_OPEN 放行一次探测 |
| `half_open_success_needed` | 2 | HALF_OPEN 连续成功 2 次才回 CLOSED |

```
CLOSED ──(窗口内失败≥3)──> OPEN ──(冷却30s)──> HALF_OPEN
   ^                                              │
   └────────(连续成功2次)──────────────────────────┘
                  HALF_OPEN 任意 1 次失败 → 立即回 OPEN
```

## 三、关键设计：配置类错误不计入熔断

`LLMGatewayError` 带一个 `count_as_failure` 标志：

| 错误类型 | 例子 | 计数 | 理由 |
|---|---|---|---|
| **瞬时故障** | 超时、连接失败、429、5xx | ✅ 计数 | 会自愈；连续出现正是熔断器要拦的 |
| **配置类故障** | key 未配置、401、402、403 | ❌ 不计数 | **重试多少次都不会好** |

为什么要区分：如果配置错误也计数，一个"忘配 key"的问题会在 3 次调用后把熔断器
打开，之后所有请求连 API 都不试、直接返回 `circuit_open`，**日志被这个假象刷满，
真正的病因被彻底淹没**。实测现场：

```
15:51:49  DEEPSEEK_API_KEY未配置，无法调用云端模型   ← 同一秒 3 次并发
15:51:49  DEEPSEEK_API_KEY未配置，无法调用云端模型
15:51:49  DEEPSEEK_API_KEY未配置，无法调用云端模型   → 触发熔断
15:52:50  ① KEY未配置（冷却到期，半开探测）
15:52:50  circuit_open ×2                             → 探测又失败，立即回 OPEN
15:53:20  circuit_open ×3
15:54:08  ① KEY未配置 → circuit_open ×3
```

前端只看到最后那句 `提供商deepseek熔断中(OPEN)`，完全看不出是"缺 key"。

分类逻辑在 `providers.py::_gateway_error()`：看 `httpx` 异常的
`response.status_code` 是否属于 `{401, 402, 403}`，是则标记
`count_as_failure=False`；缺 key 的分支直接显式标记。

回归测试：
- `test_config_error_does_not_trip_circuit_breaker`（连打 6 次配置错误，熔断器仍 CLOSED）
- `test_transient_error_still_trips_circuit_breaker`（瞬时故障仍按阈值熔断）
- `test_provider_classifies_http_status`（401/402/403 vs 429/5xx/超时）

## 四、排查手册

**现象：`全部模型调用失败（链: ...）: 提供商X熔断中(OPEN)`**

按顺序查，**不要先怀疑"模型服务挂了"**：

1. **看真实病因**，别被 `circuit_open` 带偏：
   ```bash
   python -c "
   import json
   for l in open('data/audit/llm_audit.jsonl', encoding='utf-8'):
       r = json.loads(l)
       if r.get('error'): print(r.get('timestamp',''), r['error'][:90])
   " | tail -20
   ```
2. **若是 `DEEPSEEK_API_KEY未配置`** → key 没进到进程里：
   - 确认 `.env` 里有 `DEEPSEEK_API_KEY=sk-...`（**推荐写在这里**，由
     pydantic-settings 读取，不依赖进程继承）；
   - 只设"用户级环境变量"是不够的：**进程环境变量在启动那一刻就固定**，
     之后再设的变量，已运行的进程看不到；而且 DSH Desktop 若在设置之前启动，
     从它内部拉起的新进程同样继承不到 —— 这种情况要退出重开 DSH Desktop。
3. **验证 key 本身有效**（绕过应用直连）：
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" https://api.deepseek.com/chat/completions \
     -H "Authorization: Bearer $env:DEEPSEEK_API_KEY" -H "Content-Type: application/json" \
     -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}],"max_tokens":1}'
   ```
   200 = key 有效（那问题一定在"没传进去"）。
4. **若是 429/5xx/超时** → 这才是真的 provider 侧问题，等冷却期过去即可；
   `recovery_cooldown_sec` 只有 30s，不需要重启服务。
5. **改了 `.env` 或 `models.yaml` 后必须重启后端**（`python manage.py start --replace --daemon`），
   两者都在进程启动时读入。

**现象：`任务X的token预算已耗尽（已用N/上限）`**

这是**预算配置**问题，不是模型故障。单任务累计 token 超过
`llm_token_budget_per_task`（默认 200000）时会拒绝后续 DeepSeek 调用。

实测基线（`data/audit/llm_audit.jsonl`，85 个任务）：

| 指标 | 值 |
|---|---|
| 一次正常 full 分析 | **38,207 token**（7 次调用，约 0.077 元） |
| 中位数 | 40,723 |
| p90 | 123,985 |

> 历史上默认值是 30000，**连一次正常 full 分析都装不下**，会在中途掐断 ——
> 表现为任务失败但模型完全正常。已调整为 200000（约 5 倍余量）。

预算现在主要用于兜住真正的异常，审计里能看到两类：

1. **单次输入 23 万 token** 的调用（`llm_input_char_hard_cap=6000` 似乎没在所有
   调用路径上生效，值得另行排查）；
2. **一个 trace 下 122 次 reasoning 调用、次次 `out=4096` 顶满上限**，疑似失控循环。

> 统计注意：审计日志里存在**同一批调用被记到两个 trace_id 下**的情况
> （明细字节级相同）。按 trace 聚合算消耗时若不识别重复，会把单任务开销高估一倍。

调整方式（按优先级）：
- 收窄分析范围：`analysis_type` 用 `sector` / `stock` 而不是 `full`；
- 调预算：`.env` 里设 `LLM_TOKEN_BUDGET_PER_TASK=...`；
- 靠缓存：同一问题重跑会命中 `data/llm_cache`，第二次不再烧 token。

**现象：主模型失败后没有降级到本地模型**

检查 `configs/models.yaml` 里该层的 fallback 是否换了 provider（见第一节的不变式），
以及本机 Ollama 是否在跑（`http://localhost:11434`）。
