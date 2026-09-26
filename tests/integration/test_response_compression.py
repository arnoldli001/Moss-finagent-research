"""响应压缩回归测试（2026-09-26 用户报障：事件告警打开要等 1-2 秒）。

## 故障与实测

用户口径：「事件告警 打开还是有 1-2 秒的延迟展示数据」。

排查结论：**不是数据库、不是后端逻辑，而是响应没有压缩**。
整个应用此前**没有注册任何压缩中间件**（`GZipMiddleware` 全局搜索为空），
所有 JSON 都明文发出。而对外试点走 **Cloudflare 隧道**，
实测带宽只有 **≈51 KB/s**（见 `web/src/alertsCache.ts` 记录的实测账）。
在这条链路上**响应体积直接等于加载时间**：

    告警列表（100 条，真实结构）  明文 109,526 B  →  2,139 ms @51KB/s
                                  gzip   1,870 B  →     37 ms
                                  压比 1.7%，节省 98%

告警内容是**中文事件描述 + 大量重复字段名/枚举值**，正是 gzip 压缩率最高的形态。

## 这些测试守什么

1. 大响应**必须**被压缩（否则 2 秒延迟回归）；
2. 压缩后**解压必须一致**（不能压坏数据）；
3. 小响应**不要**压（gzip 头 + CRC 就要 20 字节，小于 512B 是纯亏）；
4. 不带 `Accept-Encoding` 的客户端仍拿明文（兼容性）。
"""

from __future__ import annotations

import gzip
import http.client
import threading
import time

import pytest
from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware

#: 与 src/api/main.py 注册时保持一致的参数
_MIN_SIZE = 512
_LEVEL = 6

_PORT = 8411


def _big_payload(n: int = 100) -> dict:
    """造与真实告警同结构的负载（中文描述 + 重复字段，压比与线上一致）。"""
    desc = ("机会信号（风险分5/机会分75）：国务院常务会议加快推进大规模设备更新和"
            "消费品以旧换新，追加安排3000亿元超长期特别国债资金，利好工程机械与家电。")
    stocks = [{"code": "002371", "name": "北方华创", "impact": "positive",
               "reason": "大基金三期注册资本3440亿元，直接受益于半导体设备国产替代"}] * 3
    items = [
        {"alert_id": f"al_{i:012x}", "alert_type": "opportunity",
         "alert_level": "high", "title": f"设备更新政策加码（第{i}条）",
         "description": desc, "risk_score": 5.0, "opportunity_score": 75.0,
         "confidence": 0.82, "affected_stocks": stocks,
         "affected_industries": ["半导体", "工程机械"],
         "impact_path": "政策→资本开支→设备订单",
         "event_publish_time": "2026-09-15T08:00:00",
         "trigger_time": "2026-09-15T08:05:00", "status": "active"}
        for i in range(n)
    ]
    return {"alerts": items, "total": len(items), "unread": 12}


@pytest.fixture(scope="module")
def live_server():
    """起一个真实的 uvicorn —— 用 TestClient 量不出压缩后的线上字节数
    （httpx 会自动解压，`len(r.content)` 拿到的是解压后大小）。"""
    import uvicorn

    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=_MIN_SIZE, compresslevel=_LEVEL)

    @app.get("/api/v1/alerts")
    def _alerts() -> dict:
        return _big_payload()

    @app.get("/health/live")
    def _tiny() -> dict:
        return {"ok": True}

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=_PORT, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.1)
    if not server.started:
        pytest.skip("uvicorn 未能启动（端口占用？）")
    yield
    server.should_exit = True
    thread.join(timeout=5)


def _get(path: str, accept: str) -> tuple[int, str, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", _PORT, timeout=10)
    try:
        conn.request("GET", path, headers={"Accept-Encoding": accept})
        resp = conn.getresponse()
        body = resp.read()
        return len(body), resp.getheader("Content-Encoding") or "(none)", body
    finally:
        conn.close()


# ---------------------------------------------------------------- 压缩生效

def test_large_json_response_is_gzipped(live_server):
    """大响应必须被压缩 —— 这是 2 秒延迟的根因。"""
    n, enc, _body = _get("/api/v1/alerts", "gzip")
    assert enc == "gzip", f"大响应没有被压缩（content-encoding={enc}）"


def test_gzip_actually_shrinks_the_payload_a_lot(live_server):
    """压缩必须**真的显著变小**（线上实测 109KB → 1.9KB，节省 98%）。"""
    raw_n, raw_enc, raw_body = _get("/api/v1/alerts", "identity")
    gz_n, gz_enc, gz_body = _get("/api/v1/alerts", "gzip")
    assert raw_enc == "(none)", "identity 请求不该被压缩"
    assert gz_enc == "gzip"
    assert raw_n > 50_000, f"造的数据太小（{raw_n}B），测不出压缩收益"
    ratio = gz_n / raw_n
    assert ratio < 0.20, (
        f"压缩率不足：{raw_n:,}B → {gz_n:,}B（{ratio*100:.1f}%）。"
        "线上实测应到 ~1.7%")


def test_gzip_roundtrip_is_lossless(live_server):
    """解压后必须与明文**逐字节一致** —— 不能压坏数据。"""
    _raw_n, _raw_enc, raw_body = _get("/api/v1/alerts", "identity")
    _gz_n, _gz_enc, gz_body = _get("/api/v1/alerts", "gzip")
    assert gzip.decompress(gz_body) == raw_body


def test_transfer_time_on_slow_tunnel_is_acceptable(live_server):
    """按线上隧道带宽（51 KB/s）折算，传输时间必须落在 1 秒以内。

    这条是**面向用户体感的断言**，不是技术指标 —— 它直接对应
    "打开事件告警要等 1-2 秒"这个报障。
    """
    raw_n, _e, _b = _get("/api/v1/alerts", "identity")
    gz_n, _e2, _b2 = _get("/api/v1/alerts", "gzip")
    tunnel_bps = 51 * 1024          # 实测 51 KB/s
    raw_ms = raw_n / tunnel_bps * 1000
    gz_ms = gz_n / tunnel_bps * 1000
    assert raw_ms > 1000, "数据不够大，这个断言失去意义"
    assert gz_ms < 1000, (
        f"压缩后仍需 {gz_ms:.0f}ms（明文 {raw_ms:.0f}ms），超过 1 秒")


# ---------------------------------------------------------------- 不该压的

def test_small_response_is_not_compressed(live_server):
    """小响应不压：gzip 头 + CRC 就有 20 字节开销，小于阈值是纯亏。"""
    n, enc, _body = _get("/health/live", "gzip")
    assert n < _MIN_SIZE, f"这条响应有 {n}B，不满足'小响应'前提"
    assert enc == "(none)", "小于 minimum_size 的响应不该被压缩"


def test_client_without_accept_encoding_gets_plain(live_server):
    """不带 Accept-Encoding 的客户端仍拿明文（兼容性不能破）。"""
    n, enc, body = _get("/api/v1/alerts", "identity")
    assert enc == "(none)"
    assert body.startswith(b"{"), "明文响应应可直接解析"
    assert n > 50_000
