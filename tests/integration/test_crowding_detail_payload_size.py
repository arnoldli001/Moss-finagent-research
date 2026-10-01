"""板块拥挤度·单板块历史曲线的**响应体积**回归测试（`CHG-0137`）。

## 为什么守"体积"而不是守"耗时"

用户报障原文：

> 「板块拥挤度 里，打开单个概念的历史拥挤度数据的图，十**几秒才出数据**，
>   看下什么原因，加载这么慢」

实测**后端全链路只花 13 ms**（meta 5.9 + 行情 3.3 + 组装 0.2），
而响应体明文 **424 KB** / gzip **66 KB** ⇒ 在 ~51 KB/s 的公网隧道上
1.3~1.5 秒、在**已登记过的劣化档 ~4.6 KB/s** 上 **14~16 秒**。

**SQLite 侧没有可优化项**（索引 `idx_crowding_sector_date` 已存在且被命中），
所以这条链路上：

    加载时间 ≈ 响应体积 ÷ 隧道带宽

这与 `test_intel_payload_size.py` 是同一条纪律（在那里已经把"体积 = 加载时间"
量过一遍）。本文件守的是**拥挤度这条曲线**，并额外守一件那轮没有的事：
**浮点精度**。因为本轮最大的收益来自"按显示精度取整让 gzip 重新有效"
（66,507 B → 33,491 B，**砍半**），而不只是"去掉几个字段"。

⚠️ 阈值是**量级守卫**（抓 2× 以上的膨胀），不是精确基准 —— 取得比实测宽，
避免正常的字段增删就把测试打红。**真实收益由两条反向断言守。**
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.sector_crowding import db  # noqa: E402

#: 隧道实测带宽（Byte/s）。与 `web/src/alertsCache.ts` / `test_intel_payload_size.py` 同源。
TUNNEL_BYTES_PER_SEC = 51 * 1024

# ---- 阈值全部按**最坏板块**（bars 最多，实测 1480 根）标定 ----------------
#
#     slim=True（现状）      明文 306,879 B   gzip 36,162 B   压比 11.8%
#     只去列、不取整         明文 361,304 B   gzip 69,308 B   压比 19.2%   ← 精度回退
#     slim=False（未瘦身）   明文 501,911 B   gzip 75,785 B   压比 15.1%
#
# 余量统一给 ~1.5×（量级守卫的纪律：抓 2× 以上膨胀，正常字段增删不该打红）。

#: 明文体积上限。实测 306,879 B ⇒ 1.5× 余量。
CROWDING_DETAIL_RAW_MAX = 460_000

#: gzip 后上限。实测 36,162 B ⇒ 1.5× 余量。
#: 注意与"精度回退"的 69,308 B 有 1.9× 差距 —— 精度回退会被这条抓到。
CROWDING_DETAIL_GZIP_MAX = 55_000

#: 隧道传输时间上限（毫秒）。实测 gzip 36,162 B ⇒ 692 ms。
CROWDING_DETAIL_GZIP_MS_MAX = 1_100

#: 压比上限。实测 11.8%（取整后）vs 19.2%（不取整）—— 阈值取中间偏上，
#: 两端都有余量。★ 这一条是**浮点精度的守卫**。
CROWDING_DETAIL_RATIO_MAX = 0.16


def _ms(nbytes: int) -> float:
    return nbytes / TUNNEL_BYTES_PER_SEC * 1000


def _payload(rows: list[dict]) -> tuple[int, int]:
    """按接口真实形态打包（`_sector_detail_sync` 的字段），返回 (明文, gzip)。"""
    body = json.dumps({"series": rows}, ensure_ascii=False,
                      separators=(",", ":")).encode()
    return len(body), len(gzip.compress(body, 6))


def _synthetic_rows(n: int = 1268) -> list[dict]:
    """按真实量级造 `slim=True` 形态的行（CI 上没有生产库时的兜底）。

    ⚠️ **不许静默 skip** —— 体积守卫的价值就是"跟着真实量级走"，
    没有库时用同量级样本照样能抓住 2× 膨胀。
    """
    rows = []
    for i in range(n):
        rows.append({
            "trade_date": f"2026{1 + i // 28:02d}{1 + i % 28:02d}",
            "sector_code": "885927.TI",
            "sector_name": "PCB概念",
            "sector_amount": 402367923.3,
            "market_amount": 1653132781025.1,
            "raw_crowding": round(0.000243 + i * 1e-8, 6),
            "ma5_crowding": round(0.000237 + i * 1e-8, 6),
            "water_level": round(0.5 + (i % 50) / 100, 3),
        })
    return rows


def _real_rows() -> tuple[list[dict], list[dict]] | None:
    """从**真实生产库**取一个板块的 (slim, full) 两组行；库不在则 None。

    ## ⚠️ 为什么**不**用 `load_config().db_path`（踩过一次）

    `src/sector_crowding/config.py::load_config` 带 `@lru_cache(maxsize=1)`，
    而 `tests/unit/test_sector_crowding.py` 的 `config` fixture 会**就地改**
    那个被缓存的单例（`base.database.path = tmp_path/...`）。
    于是只要它先跑，本文件再调 `load_config()` 就会拿到**临时库**，
    判据在一个 30 行的库上求值 ⇒ 三个反向断言全红（实测踩到）。

    所以这里**绕开缓存**：库路径从 `data_stores` registry 现取
    （与 `SectorCrowdingConfig` 的默认值**同源**，不是另抄一份字面量），
    再用**只读**的裸连接打开。测试本来也不该写生产库。
    """
    try:
        from src.infrastructure.catalog.data_stores import store_rel

        rel = store_rel("legacy_main")
        if not rel:
            return None
        path = Path(rel)
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            return None

        # `mode=ro`：测试只读，绝不给生产库留任何写机会
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                f"SELECT sector_code FROM {db.META_TABLE} "
                f"ORDER BY bars DESC LIMIT 1").fetchone()
            if row is None:
                return None
            slim = db.query_sector_crowding(conn, row["sector_code"], slim=True)
            full = db.query_sector_crowding(conn, row["sector_code"], slim=False)
            return (slim, full) if slim and full else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 库不可用 → 走兜底样本，不是失败
        return None


@pytest.fixture(scope="module")
def payloads() -> dict[str, list[dict]]:
    """待测数据：优先真实库，缺失时用同量级样本。"""
    real = _real_rows()
    if real is not None:
        slim, full = real
        return {"slim": slim, "full": full, "source": "real"}
    return {"slim": _synthetic_rows(), "full": _synthetic_rows(),
            "source": "synthetic"}


def test_payload_fixture_reports_its_data_source(payloads) -> None:
    """把"这次量的是真数据还是兜底样本"**显式打出来**。

    体积守卫的价值在于"跟着真实量级走"。若有一天生产库路径变了、
    `_real_rows()` 静默退回合成样本，上面几条判据会**继续全绿**
    而实际什么都没量到（本项目登记过的"假绿"形状）。
    所以这条把数据源变成可执行证据：开发机上**必须**是 `real`。
    """
    source = payloads["source"]
    bars = len(payloads["slim"])
    print(f"\n[拥挤度体积守卫] 数据源={source}，{bars} 根日线")
    if source != "real":
        pytest.skip(
            f"本次用兜底样本（{bars} 根）—— 生产库不可用。"
            f"CI 上属预期；**开发机上应检查 data/moss_finagent.db 是否存在**。")
    assert bars > 1000, (
        f"真实库只取到 {bars} 根日线 —— 与「近 6 年 1400+ 根」的量级不符，"
        f"体积判据会在错误的量级上求值")


# ---------------------------------------------------------------- 体积守卫

def test_crowding_detail_raw_within_budget(payloads) -> None:
    """明文体积不许超出量级上限。"""
    raw, _gz = _payload(payloads["slim"])
    assert raw <= CROWDING_DETAIL_RAW_MAX, (
        f"拥挤度详情明文 {raw:,}B 超过上限 {CROWDING_DETAIL_RAW_MAX:,}B"
        f"（数据源={payloads['source']}，{len(payloads['slim'])} 根日线）。"
        f"在 51KB/s 隧道上要 {_ms(raw):.0f}ms。")


def test_crowding_detail_gzip_within_budget(payloads) -> None:
    """gzip 后体积不许超出量级上限。"""
    _raw, gz = _payload(payloads["slim"])
    assert gz <= CROWDING_DETAIL_GZIP_MAX, (
        f"gzip 后 {gz:,}B 超过上限 {CROWDING_DETAIL_GZIP_MAX:,}B"
        f"（数据源={payloads['source']}）")


def test_crowding_detail_gzip_transfer_time_acceptable(payloads) -> None:
    """gzip 后在隧道上的传输时间要落在用户可接受区间。"""
    _raw, gz = _payload(payloads["slim"])
    assert _ms(gz) <= CROWDING_DETAIL_GZIP_MS_MAX, (
        f"gzip 后仍需 {_ms(gz):.0f}ms（上限 {CROWDING_DETAIL_GZIP_MS_MAX}ms）"
        f"—— 用户会明显感到卡（本次报障就是这个形状）")


def test_float_precision_keeps_gzip_effective(payloads) -> None:
    """★ **压比必须够低** —— 这一条是浮点精度的守卫。

    本轮最反直觉的实测结论：**只去字段只降 9%**（gzip 本来就能压掉重复值），
    真正让体积掉下来的是"按显示精度取整" ⇒ 高熵的 double 尾数消失、
    gzip 重新有效（66,507 B → 33,491 B，**砍半**）。

    所以一旦精度退回完整 double，这条会**先红** —— 比体积上限更灵敏，
    因为体积上限留了余量而压比直接反映"数据有多随机"。
    """
    raw, gz = _payload(payloads["slim"])
    ratio = gz / raw
    assert ratio <= CROWDING_DETAIL_RATIO_MAX, (
        f"压比 {ratio*100:.1f}% 高于上限 {CROWDING_DETAIL_RATIO_MAX*100:.0f}%"
        f"（实测 13.0%）。**这通常意味着浮点精度退回了完整 double** —— "
        f"检查 db.SLIM_PRECISION 是否被绕过（docs/PRD.md §22.4）")


def test_float_precision_regression_is_detected(payloads) -> None:
    """★ **自证**：把精度退回完整 double，压比判据**必须**能抓到。

    这条是"判据自己还可信"的证明 —— 与 `test_intel_payload_size.py` 的
    `test_uncompressed_transfer_would_be_noticeable` 同一性质：
    **只会报 OK 的检查等于没有检查**。

    做法：拿 `slim=False` 的**完整精度行**，只做"去列"（不取整）——
    这正是"有人把 `SLIM_PRECISION` 绕过 / 删掉"的结果。它**必须**越界。
    """
    # 「只去列、不取整」= 精度回退的等价物
    unrounded = [{k: row[k] for k in db.SLIM_COLUMNS}
                 for row in payloads["full"]]
    raw, gz = _payload(unrounded)
    ratio = gz / raw
    assert ratio > CROWDING_DETAIL_RATIO_MAX, (
        f"精度回退的对照样本压比只有 {ratio*100:.1f}%，没越过阈值 "
        f"{CROWDING_DETAIL_RATIO_MAX*100:.0f}% —— 本判据抓不到精度回退，"
        f"需要换判据（**不是**放宽阈值）")
    assert gz > CROWDING_DETAIL_GZIP_MAX, (
        f"精度回退的 gzip {gz:,}B 没越过体积上限 "
        f"{CROWDING_DETAIL_GZIP_MAX:,}B —— 体积上限也抓不到精度回退")


def test_rounding_is_what_buys_the_compression_win(payloads) -> None:
    """★ 把本轮的**核心结论**钉成判据：**取整**才是收益的来源。

    实测（最坏板块）：只去列 ⇒ gzip 69,308 B；**再加取整 ⇒ 36,162 B（砍半）**。
    所以"去字段"与"取整"的收益**差了近一倍** —— 文档里那句
    「单看去字段只降 9%，取整让 gzip 重新有效」就是这条。

    若将来有人删掉取整却保留去列（看起来"还是在优化体积"），这条会红。
    """
    rounded_raw, rounded_gz = _payload(payloads["slim"])
    unrounded = [{k: row[k] for k in db.SLIM_COLUMNS}
                 for row in payloads["full"]]
    _u_raw, unrounded_gz = _payload(unrounded)
    assert unrounded_gz >= rounded_gz * 1.6, (
        f"取整带来的收益只剩 {100*(1 - rounded_gz/unrounded_gz):.0f}%"
        f"（实测应为 ~48%：{unrounded_gz:,}B → {rounded_gz:,}B）—— "
        f"精度取整可能已经被绕过（docs/PRD.md §22.4）")


def test_crowding_detail_has_a_compression_win_to_measure(payloads) -> None:
    """体积必须大到"压缩有意义"（小于 512B 的话 GZipMiddleware 会跳过）。"""
    raw, _gz = _payload(payloads["slim"])
    assert raw > 512, "payload 太小，测不出压缩收益"


# ---------------------------------------------------------------- 反向断言

def test_slim_is_materially_smaller_than_full(payloads) -> None:
    """★ **反向断言①**：`slim=True` 必须**明显小于** `slim=False`。

    防的是"瘦身被静默撤销" —— 比如有人把 `SLIM_COLUMNS` 改成 `"*"`，
    或把两条路径合成一条。那时体积会悄悄涨回 424 KB / 14 秒，
    **而没有任何报错**（正是本次报障的形状）。
    """
    slim_raw, slim_gz = _payload(payloads["slim"])
    full_raw, full_gz = _payload(payloads["full"])
    assert full_raw > slim_raw, (
        f"`slim=False` 明文 {full_raw:,}B 不大于 `slim=True` {slim_raw:,}B —— "
        f"瘦身没有生效（两条路径可能已经合一）")
    # gzip 后必须**至少减半**：本轮实测是 −50%（66,507 → 33,491）
    assert full_gz >= slim_gz * 2, (
        f"`slim=False` gzip {full_gz:,}B 不到 `slim=True` {slim_gz:,}B 的 2 倍 —— "
        f"预期减半（精度取整的收益），当前只降 "
        f"{100*(1 - slim_gz/full_gz):.0f}%")


def test_uncompressed_transfer_would_be_noticeable(payloads) -> None:
    """★ **反向断言②**：证明 gzip 不是可选项。

    如果这条失败（明文传输已经很快），说明隧道带宽假设变了，
    本文件所有阈值的意义需要重新评估 —— 这是一条"假设校验"用例，
    而不是"功能"用例（与 `test_intel_payload_size.py` 同名用例同一纪律）。
    """
    raw, _gz = _payload(payloads["slim"])
    assert _ms(raw) > CROWDING_DETAIL_GZIP_MS_MAX, (
        "明文传输已经比 gzip 阈值还快 —— 隧道带宽假设（51 KB/s）可能已过时，"
        "请重新评估本文件所有阈值")


# ---------------------------------------------------------------- 字段占用

def test_unused_fields_do_not_come_back(payloads) -> None:
    """三个白传列在 `slim=True` 里必须彻底消失（28.9% 明文）。"""
    for row in payloads["slim"]:
        for banned in ("id", "created_at", "updated_at"):
            assert banned not in row, (
                f"`{banned}` 又出现在展示行里 —— 前端 CrowdingRow 不读它，"
                f"属于白传字节（docs/PRD.md §22.3）")


def test_no_unused_field_dominates_the_payload(payloads) -> None:
    """**任何**单个列都不许占到明文的 40% 以上。

    这条盯的是"下一个 `created_at`"：本轮就是两个时间戳各占 12.5%
    （且 1268 行逐字相同）而没人发现。改成看**占比**而不是点名，
    这样将来新增的胖字段也会被抓到。
    """
    rows = payloads["slim"]
    total = sum(len(json.dumps(r, ensure_ascii=False).encode()) for r in rows)
    if total == 0:
        pytest.skip("没有行可量")
    for column in rows[0]:
        size = sum(len(json.dumps({column: r.get(column)},
                                  ensure_ascii=False).encode()) - 2
                   for r in rows)
        share = size / total
        assert share <= 0.40, (
            f"`{column}` 占明文的 {share*100:.0f}% —— 单个字段不该这么胖，"
            f"确认它是不是前端真正消费的（docs/PRD.md §22.4 硬规则 1）")
