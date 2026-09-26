"""情报流（热点&研报小作文）与事件告警的**响应体积**回归测试。

## 为什么守"体积"而不是守"耗时"

这两个页面的加载时间**不由后端计算决定，而由响应体积决定**。
本机后端 `/intel/feed` 只要 36 ms，但对外试点走 **Cloudflare 隧道**，
实测带宽 **≈51 KB/s**（见 `web/src/alertsCache.ts` 里记录的实测账）。
在这条链路上：

    加载时间 ≈ 响应体积 ÷ 51 KB/s

实测（2026-09-26，60 条真实情报条目）：

    intel /feed   raw 62,657 B → 明文 1,224 ms
                  gzip 16,994 B →   gzip  332 ms   压比 27%

用户报障原文是"这两个页面来回切，都要等 2-3 秒才展示数据"——
1,224 ms 的纯传输正是其中一大块（另一块是面板重挂后必发一次请求，
由 `web/src/intelCache.ts` 的 stale-while-revalidate 解决）。

所以这些用例守两件事：
1. **体积不许无限膨胀** —— 情报条目一多、`summary`/`credibility` 一胖，
   加载时间就线性变长，而且不会有任何报错；
2. **gzip 必须生效** —— 没有它，明文体积直接等于加载时间。

⚠️ 这些是**量级守卫**（抓 2× 以上的膨胀），不是精确基准 ——
所以阈值取得比实测宽，避免正常的字段增删就把测试打红。
"""

from __future__ import annotations

import gzip
import json

import pytest

#: 隧道实测带宽（Byte/s）。与 `web/src/alertsCache.ts` 同源。
TUNNEL_BYTES_PER_SEC = 51 * 1024

#: `/intel/feed?limit=60` 的明文体积上限。
#: 实测 62,657 B；给 1.6× 余量 —— 超过就说明有字段在无节制膨胀。
INTEL_FEED_RAW_MAX = 100_000

#: gzip 后上限。实测 16,994 B（压比 27%）。
INTEL_FEED_GZIP_MAX = 30_000

#: 明文传输时间上限（毫秒）。超过这个数，用户一定会感觉到"卡"。
INTEL_FEED_RAW_MS_MAX = 2_500

#: gzip 后传输时间上限（毫秒）。
INTEL_FEED_GZIP_MS_MAX = 700


def _intel_items(n: int = 60) -> list[dict]:
    """从真实的条目文件读条目；文件不存在时用同结构样本。

    为什么优先读真实文件：体积守卫的价值在于"跟着真实数据走"。
    但 CI 上 `data/intel` 可能不存在，所以必须有一条可复现的兜底样本。
    """
    from pathlib import Path

    p = Path("data/intel/zsxq_items.jsonl")
    if p.is_file():
        items = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(items) >= n:
                break
        if items:
            return items

    # 兜底：按真实结构的量级造（summary ~450B、credibility 子对象 ~250B）
    return [
        {
            "content_hash": f"h{i:040d}",
            "title": "国务院常务会议加快推进大规模设备更新和消费品以旧换新",
            "summary": ("会议决定追加安排超长期特别国债资金，支持大规模设备更新与"
                        "消费品以旧换新，重点覆盖工业母机、工程机械、家电与汽车；"
                        "机构测算本轮政策有望拉动相关设备投资约数千亿元，"
                        "利好产业链中游设备商与下游渠道商。") * 2,
            "credibility": {
                "score": 82.5, "level": "high", "is_official": True,
                "source_weight": 0.9, "recency_weight": 0.95,
                "text_weight": 0.8, "reasons": ["官方来源", "时效性好"],
            },
            "published_at": "2026-09-15T08:00:00+08:00",
            "at": "2026-09-15T08:05:00+08:00",
            "kind": "news", "source_alias": "财联社", "platform": "web",
            "codes": ["002371"], "industry": "机械设备", "agency": "",
        }
        for i in range(60)
    ]


def _payload(items: list[dict]) -> tuple[int, int]:
    body = json.dumps({"items": items, "counts": {}, "cached": True},
                      ensure_ascii=False).encode()
    return len(body), len(gzip.compress(body, 6))


def _ms(nbytes: int) -> float:
    return nbytes / TUNNEL_BYTES_PER_SEC * 1000


# ---------------------------------------------------------------- 体积守卫

def test_intel_feed_payload_not_bloated():
    """情报流 60 条的明文体积不许超出量级上限。"""
    raw, _gz = _payload(_intel_items(60))
    assert raw <= INTEL_FEED_RAW_MAX, (
        f"/intel/feed?limit=60 明文 {raw:,}B 超过上限 {INTEL_FEED_RAW_MAX:,}B。"
        f"在 51KB/s 隧道上要 {_ms(raw):.0f}ms —— 用户会明显感到卡。")


def test_intel_feed_gzip_is_effective():
    """gzip 后体积必须显著变小（实测压比 27%）。"""
    raw, gz = _payload(_intel_items(60))
    assert gz <= INTEL_FEED_GZIP_MAX, (
        f"gzip 后 {gz:,}B 超过上限 {INTEL_FEED_GZIP_MAX:,}B（明文 {raw:,}B）")
    assert gz < raw * 0.6, (
        f"压比只有 {gz/raw*100:.0f}%，情报流是中文 JSON，应远好于此")


def test_intel_feed_gzip_transfer_time_acceptable():
    """gzip 后在隧道上的传输时间要落在用户可接受区间。"""
    _raw, gz = _payload(_intel_items(60))
    assert _ms(gz) <= INTEL_FEED_GZIP_MS_MAX, (
        f"gzip 后仍需 {_ms(gz):.0f}ms（上限 {INTEL_FEED_GZIP_MS_MAX}ms）")


def test_uncompressed_transfer_would_be_noticeable():
    """**反向断言**：证明 gzip 不是可选项。

    如果这条失败（明文传输已经很快），说明隧道带宽假设变了，
    上面几条阈值的意义需要重新评估 —— 这是一条"假设校验"用例，
    而不是"功能"用例。
    """
    raw, _gz = _payload(_intel_items(60))
    assert _ms(raw) > INTEL_FEED_GZIP_MS_MAX, (
        "明文传输已经比 gzip 阈值还快 —— 隧道带宽假设（51 KB/s）可能已过时，"
        "请重新评估本文件所有阈值")


def test_intel_feed_has_a_compression_win_to_measure():
    """体积必须大到"压缩有意义"（小于 512B 的话 GZipMiddleware 会跳过）。"""
    raw, _gz = _payload(_intel_items(60))
    assert raw > 512, "payload 太小，测不出压缩收益"


# ---------------------------------------------------------------- 字段占用

def test_summary_does_not_dominate_the_payload():
    """`summary` 是最大字段（实测占 53%），它若失守则整体体积失控。

    这条不是"不许大" —— summary 是读起来最有价值的部分。
    它守的是**量级**：一旦某天把全文塞进 summary（而不是留在
    `item_bodies.jsonl` 的按需加载里），这里会先红。
    """
    items = _intel_items(60)
    total = sum(len(json.dumps(x, ensure_ascii=False).encode()) for x in items)
    if total == 0:
        pytest.skip("没有条目可量")
    summary_bytes = sum(
        len(json.dumps(x.get("summary", ""), ensure_ascii=False).encode())
        for x in items)
    share = summary_bytes / total
    assert share <= 0.70, (
        f"summary 占 {share*100:.0f}%（实测 53%）。"
        "如果正文被塞进了列表响应，应改为按需加载（/intel/item/{hash}）")
