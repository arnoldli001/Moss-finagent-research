import { useCallback, useEffect, useRef, useState } from "react";
import { alertsWsUrl, Alert, api } from "../api";

/**
 * 事件告警WebSocket（自动重连，3秒退避；快照/增量两种消息）。
 * 单例连接挂在App层，铃铛未读数与Toast共用，避免多组件多连接。
 *
 * ## ★ 同一条连接还承载"情报流已就绪"的信令（2026-10-01 第七轮）
 *
 * 情报流的采集在服务端是**后台任务**（`/feed` 立刻返回，见后端
 * `intel.py`），所以前端需要"建好了叫我一声"。这里**刻意复用既有连接**：
 * 再开一条 WebSocket 就要把重连、25 秒心跳、登录门槛（`ws_allow`）、
 * 租户分组全部再实现一遍，而两条通道迟早会在某一次改动后表现不一致。
 *
 * ⚠️ `intel_feed` 的载荷**只有序号与时间戳**，没有一条情报内容：
 * 它是"可以去拉了"的信令，内容仍然走 `GET /feed` 那一条路
 * （情报流的渲染路径**没有**增加第二条）。
 */
export function useAlertsWs() {
  const [connected, setConnected] = useState(false);
  const [unread, setUnread] = useState(0);
  const [incoming, setIncoming] = useState<Alert | null>(null);
  /**
   * 情报流数据序号。服务端每次成功重建情报流就 +1，`0` 表示"还没有过"。
   *
   * 消费方（`IntelPanel`）拿它与手里那份的 `seq` 比较：更大 → 说明服务端
   * 有了一份新的，于是自己再拉一次。**不做成回调**：回调会让"组件还没挂载
   * 时到达的通知"永久丢失，而序号是幂等的状态，晚订阅也能看到。
   */
  const [intelSeq, setIntelSeq] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);
  const closedByMe = useRef(false);
  const retryTimer = useRef<number | null>(null);
  const pingTimer = useRef<number | null>(null);
  const retries = useRef(0);

  const refreshUnread = useCallback(() => {
    api.unreadCount()
      .then((r) => setUnread(r.unread))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    const connect = () => {
      const ws = new WebSocket(alertsWsUrl());
      wsRef.current = ws;
      ws.onopen = () => {
        retries.current = 0;
        setConnected(true);
        if (pingTimer.current !== null) window.clearInterval(pingTimer.current);
        // 双向保活：客户端每25秒发ping，防止代理回收空闲连接
        pingTimer.current = window.setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "ping" }));
          }
        }, 25000);
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as
            | { type: "snapshot"; unread: number; data: Alert[] }
            | { type: "alert"; data: Alert }
            | { type: "intel_feed"; data: { seq: number; built_at: string } }
            | { type: "heartbeat"; ts: string };
          if (msg.type === "snapshot") {
            setUnread(msg.unread);
          } else if (msg.type === "alert") {
            setUnread((n) => n + 1);
            setIncoming(msg.data);
          } else if (msg.type === "intel_feed") {
            // 只往大取：乱序/重放的消息不许把序号**推回去** ——
            // 推回去会让面板以为"没有新数据"，于是停在旧内容上（且不报错）。
            const seq = Number(msg.data?.seq ?? 0);
            if (Number.isFinite(seq) && seq > 0) {
              setIntelSeq((n) => (seq > n ? seq : n));
            }
          }
          // heartbeat仅用于保活探活，无业务处理
        } catch { /* 忽略非JSON帧 */ }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closedByMe.current) {
          retries.current = Math.min(retries.current + 1, 8);
          retryTimer.current = window.setTimeout(
            connect, 3000 * retries.current);
        }
      };
      ws.onerror = () => ws.close();
    };
    closedByMe.current = false;
    connect();
    return () => {
      closedByMe.current = true;
      if (retryTimer.current !== null) window.clearTimeout(retryTimer.current);
      if (pingTimer.current !== null) window.clearInterval(pingTimer.current);
      wsRef.current?.close();
    };
  }, []);

  return { connected, unread, incoming, intelSeq, setUnread, refreshUnread };
}
