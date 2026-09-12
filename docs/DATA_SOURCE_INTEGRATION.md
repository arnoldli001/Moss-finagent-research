# 数据源集成指南

## 一、数据源清单

| 数据类型 | 数据源 | 获取方式 | 更新频率 | 优先级 |
|----------|--------|----------|----------|--------|
| 宏观经济 | 国家统计局 data.stats.gov.cn | API/网页 | 随官方发布 | P0 |
| CPI/PPI | 国家统计局数据发布库 | API | 次月10日左右 | P0 |
| 央行数据 | 央行 www.pbc.gov.cn | 网页 | 月度/季度 | P0 |
| 全球流动性 | FRED fred.stlouisfed.org | API | 实时 | P1 |
| 厄尔尼诺 | NOAA psl.noaa.gov | 文件下载 | 月度/周度 | P1 |
| A股行情 | AkShare / Tushare Pro / BaoStock | API | 实时/日频 | P0 |
| 券商研报 | 慧博投研 www.hibor.com.cn | 订阅 | 实时 | P1 |
| 大宗商品 | 生意社 www.100ppi.com | 网页 | 日频/周频 | P2 |

## 二、连接器实现规范

每个数据源实现一个连接器，放在infrastructure/data_access/connectors/下：

```python
class BaseConnector(ABC):
    @abstractmethod
    async def fetch(self, indicator: str, start_date: str, end_date: str) -> list[DataPoint]:
        pass

    @abstractmethod
    def get_capabilities(self) -> dict:
        pass

class StatsGovConnector(BaseConnector):
    async def fetch(self, indicator: str, start_date: str, end_date: str):
        # 调用国家统计局API
        # 返回DataPoint列表（含溯源元数据）
        pass
```

## 三、AkShare使用示例

```python
import akshare as ak

# 获取CPI数据
cpi_df = ak.macro_china_cpi_monthly()

# 获取PPI数据
ppi_df = ak.macro_china_ppi_yearly()

# 获取A股行情
stock_df = ak.stock_zh_a_hist(symbol="000001", period="daily")
```

## 四、Tushare使用示例

```python
import tushare as ts

pro = ts.pro_api('your_token')

# 获取日线行情
df = pro.daily(ts_code='000001.SZ', start_date='20260101', end_date='20260912')

# 获取财务数据
df = pro.balancesheet(ts_code='000001.SZ', start_date='20260101')
```

## 五、数据源降级策略

| 主数据源 | 备用数据源 | 降级条件 |
|----------|-----------|----------|
| Tushare | AkShare | API限流或超时 |
| AkShare | BaoStock | 接口异常 |
| 国家统计局API | 网页爬取 | API不可用 |
