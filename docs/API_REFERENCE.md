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
