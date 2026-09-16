# 量化数据仓库：集中管理 + 去重 + 回测取数加速

> 目标读者：后续维护 M1/M3/M4 的人，以及想知道"数据到底存在哪、为什么不重复"的使用者。
> 所有数字都是本机实测（2026-09-16），不是估算。

## 1. 数据放在哪：三层，各管一件事

```
Tushare API ──下载──▶ CSV 分区缓存 ──入库──▶ 数据库表
（采集，受 500 次/分限流）  （原始落地，可重放）  （查询层，回测读这里）
data/quant/tushare/…      data/quant/warehouse.db
                          （或 MySQL/PostgreSQL）
```

**为什么保留 CSV 而不直接只存库**：CSV 分区是"原始落地层"，
schema 变了、入库写错了、想换库，都能从它重放；数据库是"派生层"，
随时可以删掉重建。反过来只存库的话，一次写坏就没有干净的源了。

## 2. 取数速度：实测结论（不是"库一定更快"）

**先说结论：入库本身不会让取数变快，快在"把过滤下推到 SQL"。**
下面是本机实测（SQLite，`quant_daily` 1538 万行 / 2006-01-01~2026-09-15，
每个数取 3~5 次平均）：

| 取数方式 | 耗时 | 相对 CSV |
|----------|-----:|---------:|
| CSV 单截面（1 日全市场 ~5000 票） | 9.1 ms | 1.00× |
| 库　单截面（同一天） | 11.0 ms | 1.22×（**略慢**） |
| CSV 250 日全列 | 2258 ms | 249× |
| 库　250 日全列 | 2908 ms | 321×（**更慢**） |
| 库　250 日 3 列（**列裁剪**） | 1050 ms | 116×（**快 2.2×**） |
| CSV 250 日 + 筛 50 只票（取完再筛） | 2313 ms | 255× |
| 库　250 日 3 列 + 筛 50 只票 | **10.3 ms** | **1.14×（快 225×）** |

怎么读这张表：

- **单截面打平**（9.1 vs 11.0 ms）：一个分区文件的 gzip 解析和一次索引命中
  是同一量级，没有"数量级提升"可言；
- **全列大区间，库反而更慢**：SQLite 的 Python 驱动 `fetchall` 实测
  **568 行/ms**，125 万行就要 2.3s —— 这已经是驱动层的下限，
  而 CSV 是 C 层解压 + 解析，并不输；
- **列裁剪立竿见影**（2908 → 1050 ms）：取数耗时几乎全在"搬多少列"，
  不在 SQL 解析。所以面板取数一律只 SELECT 需要的三列（日期/代码/字段）；
- **代码筛选是数量级差距**（2313 → 10.3 ms）：`(code, trade_date)` 复合索引
  直接定位，而 CSV 只能把 250 个文件全读进来再筛。**这是本地库最大的价值**。

> 踩过的坑：最初用 `pd.read_sql_query(sql, sqlalchemy_connection)`，
> 得到的是 SQLAlchemy `Row` 对象再逐行转 DataFrame —— 同样一条 SQL
> 多花 ~4ms/5000 行、~2.5s/125 万行。改用底层 DBAPI 连接
> （`raw_connection().driver_connection`）后才是上表数字。

## 3. 面板装配：真正的瓶颈不在取数

`build_panels` 要为 35 个因子装配 40+ 个字段。原实现**每个字段都把整个数据集
重读一遍**（40 天面板 = 41 字段 × 40 分区 ≈ 1666 次 `read_csv`，
另有 15.7 万次文件存在性 `stat`）。逐步实测（171 日 × 5238 只票）：

| 阶段 | 耗时 | 关键动作 |
|------|-----:|----------|
| 起点 | 103.9 s | 每字段独立试库 + 每字段重扫 CSV 分区 |
| ① ② ③ | 31.1 s | ①每个数据集只定位一次来源（库 or CSV）；②分区只读一次再切字段；③向量化 `pit.to_yyyymmdd` |
| ④ | 42.5 s ⚠️ | 把 `daily_basic` 也入了库，**反而更慢**：16 个字段各自查一次库（每次搬 88 万行），比整表读一次还贵 |
| ⑤ | 29.4 s | 按**数据集**一次取回需要的列（缓存键是数据集，不是字段） |
| ⑥ | **27.3 s** | 删掉 `BASIC_FIELDS` 里那个源根本不返回、也没人用的 `limit_status`（它让每次装配都白读 171 个分区去找一个不存在的列） |
| 再装配一次（PIT 财务面板命中缓存） | **19.5 s** | 回测换参数的循环场景 |

三条可迁移的教训：

1. **"入了库就更快"是错的**，取决于取数形态（见 §2）；
2. **按字段查库是反模式**：一次查询的固定成本 × 字段数，会轻松吃掉索引的收益；
3. **一个不存在的列可以很贵**：字段清单里留一个永远为空的字段，
   代价是每次装配多读一遍整个数据集。

> 注意：① ② ③ ⑥ 对 CSV 路径同样有效，与是否入库无关。
> 把"入库"和"取数实现"混在一起谈，就得不出"入库到底值不值"的结论。

### 3.1 剩余可优化项（未做，如实记录）

- `pit.to_yyyymmdd` 向量化后仍是 4.2s/百万行（pandas 字符串链式操作的固有成本）；
  已用"按数据身份缓存 PitPanel"绕开，但首次装配仍要付这个钱。
- `PitPanel._prepare` 占 7.5s，其中 `merge_asof` 逐期对齐可考虑改成
  一次性排序 + 二分查找。
- 剩下 6 个数据集（adj_factor / moneyflow / stk_limit / bak_daily / suspend_d /
  index_daily）入库后装配还能再降一截（约 1200 次分区读取 → 6 次带投影的查询）。


## 4. 去重：靠唯一键，不靠"导入前先查一遍"

"先去重再插入"这种写法有两个致命问题：检查与插入之间有时间窗（并发会重复）、
断点重跑时要全表扫一遍。**这里把去重下沉到数据库约束**：

| 数据集类型 | 去重键（= 唯一键） | 语义 |
|------------|-------------------|------|
| 日频（daily / daily_basic / adj_factor / moneyflow / stk_limit / suspend_d / bak_daily / index_daily） | `(trade_date, code)` | 同一只票同一天只可能有一行 |
| 财务（fina_indicator_vip） | `(code, report_period, ann_date)` | **保留同一报告期的多个公告版本** |
| 基础信息（stock_basic） | `(code,)` | 一只票一行 |

写入用方言原生 UPSERT，**重复导入是幂等的**（覆盖而不是报错），
所以"跑到一半断了再跑一遍"是安全的操作，不需要先清理。

### 财务表为什么不能只留最新版

如果按 `(code, report_period)` 去重只留最新公告，那么"2026-01-20 的截面"
就会用上 2026-04-10 才公告的修正数字 —— 这是**未来函数**，
回测收益会凭空变好而无法在实盘复现。所以去重键必须包含 `ann_date`，
多个版本共存，由 PIT 面板在查询时按 `ann_date <= as_of` 选出当时可见的那一版。

## 5. 方言无关：MySQL / PostgreSQL / SQLite 同一套代码

选库的优先级（`WarehouseConfig.from_env`）：

1. `MOSS_DB_URL` / `MOSS_QUANT_DB_URL` / `QUANT_DB_URL` —— 完整连接串；
2. `MOSS_MYSQL_HOST/PORT/USER/PASSWORD/DATABASE`（或项目 `.env` 里的同名字段）；
3. `MOSS_QUANT_SQLITE` / 缺省 `data/quant/warehouse.db`。

UPSERT 语法按方言生成（MySQL `ON DUPLICATE KEY UPDATE`、
PostgreSQL/SQLite `ON CONFLICT … DO UPDATE`），**改库不用改代码**。

> **本机现状（诚实说明）**：MySQL 服务在跑，但 `finagent-research` 只有 SQLite、
> `moss-finance-assistant` 的凭据是占位符（`your-db-user`），
> 项目默认 PostgreSQL DSN 也连不上；而 `.venv` 里没有同步 PostgreSQL 驱动。
> 因此当前默认落在 SQLite —— 对单机研究型负载它其实更快（无网络往返、
> 无连接池开销），且项目本身 `DATA_BACKEND=sqlite` 就是默认值。
> 凭据一到位，设一个环境变量即可切 MySQL 并重跑 `ingest`（幂等，不会重复）。

## 6. 怎么用

```bash
# 看连接与各表覆盖
uv run python scripts/quant_warehouse.py status

# 全量入库（幂等，可随时重跑）
uv run python scripts/quant_warehouse.py ingest
uv run python scripts/quant_warehouse.py ingest --dataset daily --dataset daily_basic

# 只补增量（按分区键过滤，不重扫全部文件）
uv run python scripts/quant_warehouse.py ingest --since 20260901

# 对比取数耗时
uv run python scripts/quant_warehouse.py bench --dataset daily_basic --days 5

# 直接查（回测走的就是这条路径）
uv run python scripts/quant_warehouse.py query --dataset daily_basic \
    --start 20260901 --code 000001 --limit 20
```

代码侧统一入口：

```python
from src.quant.warehouse import load_dataset

frame, origin = load_dataset("daily_basic", start="20260101", end="20260915")
# origin == "db:sqlite.quant_daily_basic" 或 "csv:daily_basic(171 个分区)"
```

**`origin` 必须一路带进回测结果**：否则"这次到底读的库还是文件"会变成
一个事后查不出来的问题，而回测复现性依赖它。

## 7. 与其它模块的关系

| 模块 | 关系 |
|------|------|
| `src/quant/download.py` | 负责从 Tushare 拉数据落到 CSV 分区（采集层） |
| `src/quant/dataset_store.py` | CSV 分区读写（含跨解释器双格式读取） |
| `src/quant/warehouse.py` | 集中管理 + 去重 + 查询（本文件描述的对象） |
| `src/quant/pit.py` | 在仓库/CSV 之上做 PIT 时点还原（ann_date 语义） |
| `src/quant/panels.py` | 组因子面板：**仓库优先，CSV 兜底**，来源写进 origins |
| `src/api/data_health.py` | 前端「数据健康度」暴露仓库行数/跨度/方言 |

## 8. 已知限制

- **不做增量调度**：`download` 与 `ingest` 都是手动/脚本触发；
  真正常态化需要挂到项目 scheduler 上（现在只有 `--since` 支持增量）。
- **没有分区表**：`quant_daily` 单表 1400 万行级；再涨（分钟线入库）
  就需要按年分区或分表。
- **索引在建表后补**：新列出现时索引会重建检查，超大表上会有一次开销；
  数据量再上一个量级要改成显式迁移脚本。
- **财务表按 `report_period` 建日期索引**：按公告日 `ann_date` 过滤的场景
  走不到该索引，PIT 面板是在内存里筛的（数据量下可接受）。
