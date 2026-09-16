import { useCallback, useEffect, useRef, useState } from "react";
import { alertsWsUrl, Alert, api } from "../api";

/**
 * 事件告警WebSocket（自动重连，3秒退避；快照/增量两种消息）。
 * 单例连接挂在App层，铃铛未读数与Toast共用，避免多组件多连接。
 */
export function useAlertsWs() {
  const [connected, setConnected] = useState(false);
  const [unread, setUnread] = useState(0);
  const [incoming, setIncoming] = useState<Alert | null>(null);
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
            | { type: "heartbeat"; ts: string };
          if (msg.type === "snapshot") {
            setUnread(msg.unread);
          } else if (msg.type === "alert") {
            setUnread((n) => n + 1);
            setIncoming(msg.data);
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

  return { connected, unread, incoming, setUnread, refreshUnread };
}
