# API参考

## 一、投研分析接口

### POST /api/v1/research/analyze

提交投研分析任务。

**请求体**：

```json
{
  "query": "分析当前煤炭行业的投资机会",
  "analysis_type": "industry",
  "target": "煤炭",
  "tenant_id": "tenant_001",
  "options": {
    "include_trace": true,
    "report_format": "markdown"
  }
}
```

**响应体**：

```json
{
  "task_id": "task_20260912_001",
  "status": "completed",
  "conclusion": "看好上游资源品（煤炭、有色）",
  "confidence": "high",
  "report": "...",
  "trace_id": "trace_20260912_001",
  "data_sources": ["data.stats.gov.cn", "pbc.gov.cn"]
}
```

## 二、数据查询接口

### GET /api/v1/data/macro

查询宏观经济数据。

**参数**：

- indicator：指标名称（PPI/CPI/PMI等）
- start_date：开始日期
- end_date：结束日期

**响应体**：

```json
{
  "data_points": [
    {
      "data_id": "d_20260912_ppi_001",
      "data_name": "PPI同比增速",
      "data_value": 3.8,
      "unit": "%",
      "source_url": "data.stats.gov.cn",
      "publish_time": "2026-09-10T09:30:00+08:00",
      "confidence": 0.95
    }
  ]
}
```

## 三、推理路径查询接口

### GET /api/v1/trace/{trace_id}

查询指定Trace的完整推理路径。

**响应体**：

```json
{
  "trace_id": "trace_20260912_001",
  "agent_id": "A08_macro",
  "reasoning_steps": [
    {
      "step": 1,
      "step_type": "data_retrieval",
      "description": "获取PPI同比数据",
      "data_refs": ["d_20260912_ppi_001"]
    }
  ],
  "conclusion": "...",
  "audit": {
    "hash": "sha256:...",
    "previous_trace_hash": "sha256:..."
  }
}
```

## 四、报告生成接口

### POST /api/v1/report/generate

生成投研报告。

**请求体**：

```json
{
  "task_id": "task_20260912_001",
  "template": "stock_deep_dive",
  "format": "markdown",
  "sections": ["summary", "financials", "valuation", "catalysts", "risks"]
}
```

## 五、健康检查接口

### GET /api/v1/health

**响应体**：

```json
{
  "status": "healthy",
  "agents": {
    "A08_macro": "healthy",
    "A10_micro": "healthy"
  },
  "data_sources": {
    "stats_gov": "connected",
    "akshare": "connected"
  }
}
```

## 六、定时调度接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/scheduler/jobs` | 作业注册表（cron/kind/暂停态/最近一次运行） |
| POST | `/api/v1/scheduler/jobs/{name}/run` | 手动触发作业（202，进程内执行并落运行记录） |
| GET | `/api/v1/scheduler/runs?job=&limit=` | 运行记录（JSONL持久化） |
| GET | `/api/v1/scheduler/runs/summary?date=` | 当日汇总（成功/失败/跳过/平均耗时） |

## 七、LLM运行指标接口

### GET /api/v1/metrics?limit=1000

**响应体要点**：`metrics.window_calls`、`cache_hit_rate`、`fallback_rate`、
`latency_ms.{p50,p95,p99,avg,max}`（nearest-rank百分位）、
`by_provider[]`（各模型供应商的错误率/缓存命中率/P95）、`by_agent[]`。

## 八、策略回测接口

### POST /api/v1/backtest/run

纯本地规则回测（**全程无LLM、无未来函数**）：实时拉取宏观指标与个股全历史行情，
按指标**发布月**与月末收盘价内连接对齐，逐月用仅含截至当月历史的扩展窗口生成信号，
计算方向命中率与多头策略净值。全历史行情拉取可能耗时数十秒。

**请求体**：

```json
{ "indicator": "PPI", "code": "601088", "eps_pct": 1.0, "pe_watermark": null }
```

- `indicator`：当前支持 `CPI` / `PPI`
- `code`：6位A股代码
- `eps_pct`：指标同比序列环比变动阈值（百分点），超过且PE不超水位线→次月持有

**响应体要点**：`range`/`periods`、`cache.{indicator_hit,price_hit,ttl_seconds}`
（全历史行情进程内TTL缓存10分钟，命中时秒回，月末月度数据短期不变）、
`signals.{long,neutral,avoid}`、
`directional`（1m/3m/6m看多/回避命中率与全程持有基准）、
`strategy`（累计/年化CAGR/年化波动/最大回撤/夏普/持仓月数，内嵌
`buy_and_hold` 与 `excess_cumulative_return`）、`equity_curve[]`、
`disclaimer`（固定风险声明，前端必须原样展示）。

**错误码**：400 参数非法；502 数据源获取失败；422 对齐月份不足（<8个月）。
API层不静默回退合成数据。

> 实测（601088，2007-10~2025-08共213月）：朴素PPI动量规则累计-46.09%，
> 买入持有+20.38%，超额-66.47%——框架验证成立，但该规则本身无超额收益。
> ⚠️ 历史回测不代表未来收益，不构成投资建议。
