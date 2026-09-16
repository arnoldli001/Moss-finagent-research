type Props = {
  unread: number;
  connected: boolean;
  onClick: () => void;
};

/** 顶部全局告警铃铛：未读红点 + WS连接状态点。 */
export default function AlertBell({ unread, connected, onClick }: Props) {
  return (
    <button
      className="alert-bell"
      onClick={onClick}
      title={connected ? "事件告警实时连接正常" : "事件告警实时连接断开（重连中）"}
      aria-label="事件告警"
    >
      <span className="bell-icon" aria-hidden="true">🔔</span>
      {unread > 0 && (
        <span className="bell-badge">{unread > 99 ? "99+" : unread}</span>
      )}
      <span className={`bell-dot ${connected ? "on" : "off"}`} />
    </button>
  );
}
