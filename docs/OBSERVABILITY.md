# 可观测性设计

## 一、Trace记录规范

每个Agent的每次调用必须记录Trace：

```json
{
  "trace_id": "trace_20260912_001",
  "agent_id": "A08_macro",
  "model_info": {
    "model_name": "deepseek-v4-flash",
    "temperature": 0.1
  },
  "task": {
    "task_id": "task_20260912_001",
    "task_type": "宏观周期定位",
    "user_query": "当前煤炭行业的投资机会分析"
  },
  "reasoning_steps": [
    {
      "step": 1,
      "step_type": "data_retrieval",
      "description": "获取PPI同比数据",
      "data_refs": ["d_20260912_ppi_001"],
      "timestamp": "2026-09-12T09:30:01+08:00",
      "duration_ms": 120
    }
  ],
  "conclusion": {
    "summary": "PPI拐点向上信号确认，看好上游资源品",
    "confidence": "high",
    "data_sources": ["data.stats.gov.cn"]
  },
  "audit": {
    "hash": "sha256:...",
    "previous_trace_hash": "sha256:...",
    "created_at": "2026-09-12T09:30:05+08:00"
  }
}
```

## 二、推理步骤类型

| 步骤类型 | 说明 | 必须记录的内容 |
|----------|------|----------------|
| data_retrieval | 数据获取 | 数据ID、数据源、获取时间 |
| indicator_calculation | 指标计算 | 公式、输入数据、输出值 |
| signal_trigger | 信号触发 | 规则ID、条件表达式、结果 |
| llm_inference | LLM推理 | Prompt哈希、Token数、输出 |
| cross_validation | 交叉验证 | 验证方法、验证结果 |
| final_conclusion | 最终结论 | 结论摘要、置信度 |

## 三、性能监控

- 每次API调用记录：request_id、agent_id、start_time、end_time、duration_ms、status。
- P50/P95/P99延迟实时监控。
- P95延迟超过5秒时触发告警。
- 慢查询日志：超过3秒的查询记录完整推理路径和耗时分解。

**实现状态**：`src/infrastructure/observability/metrics.py` 从
`data/audit/llm_audit.jsonl` 聚合（nearest-rank分位）：窗口调用数、
P50/P95/P99/平均/最大延迟、≥3秒慢调用数、缓存命中率、降级率、错误率、
Token累计、按提供方与按Agent分解。经 `GET /api/v1/metrics?limit=1000`
暴露，前端"运行指标"视图15秒自动刷新。阈值告警（P95>5s主动推送）尚未实现，
当前为可查询面板。

## 四、审计日志存储

| 数据类型 | 存储方案 | 保留期限 |
|----------|----------|----------|
| 原始数据快照 | 本地文件（WORM） | 3年 |
| 溯源元数据 | SQLite | 3年 |
| 推理路径Trace | JSON文件 + SQLite | 3年 |
| 审计日志 | 本地文件 + 哈希链 | 3年 |

## 五、哈希链设计

```
Trace_N.hash = SHA256(Trace_N.content + Trace_N-1.hash)
```

每条Trace记录包含前一条Trace的哈希值，形成链式结构，任何修改都会破坏链的完整性。
