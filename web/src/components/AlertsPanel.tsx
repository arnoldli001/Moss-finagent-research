import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, AlertSettings, ScanState, api } from "../api";
import {
  filterKeyOf, readAlerts, readAlertSettings, writeAlerts,
} from "../alertsCache";
import AlertDetail from "./AlertDetail";

type Props = {
  incomingTick: number;
  openAlertId: string | null;
  onConsumeOpen: () => void;
  onReadChanged: () => void;
};

/**
 * 方向标签：**利空 / 利多**（不是"风险 / 机会"）。
 *
 * 用户口径与本项目合规基线：平台只描述公开信息，不给操作暗示。
 * "机会"是带操作暗示的词（暗示"该买"），而"利空/利多"是对
 * **第三方原文语气**的事实描述 —— 与情报流里的 `tone` 同一套措辞。
 *
 * ⚠️ 底层枚举值仍是 `risk` / `opportunity`（后端、数据库、筛选参数都用它），
 * 这里只改**展示文案** —— 改枚举会牵动库里的历史数据与所有筛选链接。
 */
const TYPE_LABEL: Record<string, string> = {
  risk: "利空", opportunity: "利多",
};
const LEVEL_LABEL: Record<string, string> = {
  high: "高", medium: "中", low: "低",
};

export default function AlertsPanel(
  { incomingTick, openAlertId, onConsumeOpen, onReadChanged }: Props,
) {
  const [settings, setSettings] = useState<AlertSettings | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [type, setType] = useState("");
  const [level, setLevel] = useState("");
  const [status, setStatus] = useState("");
  const [selected, setSelected] = useState<Alert | null>(null);
  const [scan, setScan] = useState<ScanState | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [form, setForm] = useState({ title: "", content: "", type: "policy" });
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    const fkey = filterKeyOf(type, level, status);
    // ★ **先画缓存、再后台核对**（stale-while-revalidate）。
    //
    // 用户口径（2026-09-26）："切网站内界面再切回也要 2 秒显示"。
    // 量出来的原因很硬：隧道实测只有 ~51 KB/s，而列表是 112 KB ——
    // 每次切回来都重走一遍隧道就是 2 秒起。可这份数据**刚刚才在手里**
    // （上一次打开时拿到的），没有任何理由扔掉重拿。
    //
    // 所以：有缓存就立刻画出来，同时发请求核对；拿到新的再覆盖。
    // 已经画了旧数据时**不显示"加载中…"** —— 那会让"立刻可用"又变回
    // "看起来在等"，等于白做。
    const cached = readAlerts(fkey);
    if (cached) setAlerts(cached.alerts);
    setLoading(!cached);
    setError(null);
    try {
      const resp = await api.alerts({
        type: type || undefined, level: level || undefined,
        status: status || undefined, limit: 100,
      });
      setAlerts(resp.alerts);
      // 存的是**这一档**的结果（键含筛选），否则"仅未读"会拿到"全部"
      writeAlerts(fkey, resp);
    } catch (e) {
      // 有缓存时不要把用户已经看到的内容换成一句错误 —— 后台核对失败
      // 不该让他手里的列表消失。静默保留缓存内容即可。
      if (!cached) setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [type, level, status]);

  useEffect(() => { void load(); }, [load, incomingTick]);

  useEffect(() => {
    // ★ 先用预加载带回来的设置，**省掉一次隧道往返**。
    //
    // 用户报障（2026-09-26）："事件告警 打开还是有 1-2 秒的延迟"。
    // 根因是响应没压缩（已在 `src/api/main.py` 注册 GZipMiddleware，
    // 实测 109KB → 1.9KB）。这里是**第二笔**可省的开销：
    // 面板打开时又单独发了一条 `/alerts/settings`，而登录预加载走的
    // `/alerts/bootstrap` **已经把 settings 一起取回来了**（见
    // `alertsCache.preloadAlerts`）。在 ~51 KB/s 的隧道上这是一次纯浪费的往返。
    //
    // 有缓存设置就直接用、**不再发请求**；没有（未登录预加载 / 缓存过期）
    // 才回退到正常拉取 —— 行为与优化前一致。
    const cachedSettings = readAlertSettings(filterKeyOf("", "", ""));
    if (cachedSettings) {
      setSettings(cachedSettings);
      return;
    }
    api.alertSettings().then(setSettings).catch(() => undefined);
  }, []);

  // 从Toast跳转进来时拉取详情
  useEffect(() => {
    if (!openAlertId) return;
    api.alertDetail(openAlertId)
      .then((r) => setSelected(r.alert))
      .catch(() => undefined)
      .finally(onConsumeOpen);
  }, [openAlertId, onConsumeOpen]);

  const stopPoll = () => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };
  useEffect(() => stopPoll, []);

  const scanNow = async () => {
    try {
      await api.triggerScan();
      setScan({ running: true, started_at: new Date().toISOString(), result: null });
      stopPoll();
      pollRef.current = window.setInterval(async () => {
        const state = await api.scanLatest();
        setScan(state);
        if (!state.running) {
          stopPoll();
          void load();
          onReadChanged();
        }
      }, 1500);
    } catch (e) {
      setError(`扫描启动失败：${String(e)}（上一次扫描可能仍在执行）`);
    }
  };

  const selectAlert = async (alert: Alert) => {
    setSelected(alert);
    if (alert.status === "active") {
      try {
        await api.markAlertRead(alert.alert_id);
        setAlerts((prev) => prev.map((a) => a.alert_id === alert.alert_id
          ? { ...a, status: "read" } : a));
        setSelected({ ...alert, status: "read" });
        onReadChanged();
      } catch { /* 并发已读时忽略 */ }
    }
  };

  const readAll = async () => {
    await api.markAllAlertsRead();
    void load();
    onReadChanged();
  };

  const submitImport = async () => {
    if (!form.title.trim()) return;
    try {
      const r = await api.importEvents([{
        title: form.title.trim(), content: form.content.trim(),
        event_type: form.type,
      }]);
      setImportMsg(`已接收${r.received}条，新入库${r.inserted}条`
        + `（点击"立即扫描"进行AI分析）`);
      setForm({ title: "", content: "", type: "policy" });
    } catch (e) {
      setImportMsg(`导入失败：${String(e)}`);
    }
  };

  if (settings && !settings.available) {
    return (
      <div className="panel">
        <h2>事件告警</h2>
        <div className="error-box">事件告警子系统不可用：当前数据后端暂仅支持 SQLite。</div>
      </div>
    );
  }

  return (
    <div className="alerts-layout">
      <div className="panel alerts-list-panel">
        <div className="alerts-toolbar">
          <h2>事件告警</h2>
          <button className="scan-btn" onClick={scanNow}
            disabled={scan?.running}>
            {scan?.running ? "扫描中…" : "立即扫描"}
          </button>
          <button className="secondary-btn" onClick={readAll}>全部已读</button>
          <button className="secondary-btn" onClick={() => setImportOpen((v) => !v)}>
            手工导入
          </button>
        </div>

        {settings && (settings.schedule?.slots?.length ?? 0) > 0 && (
          <div className="info-box">
            自动扫描时机：{settings.schedule.weekday_only ? "工作日 " : ""}
            {settings.schedule.slots!.join(" / ")}
            {settings.schedule.startup_scan && "，服务启动后再自动补扫一次"}
            {" · "}也可点「立即扫描」手动触发
          </div>
        )}

        {settings && !settings.email.configured && (
          <div className="info-box warn-box">
            邮件通道未配置（在 .env 设置 ALERT_SMTP_USER / ALERT_SMTP_AUTH_CODE 后重启），
            当前仅站内实时推送；配置后利空分&gt;{settings.email.risk_min_score}
            或利多分≥{settings.email.opp_min_score} 的告警将发送至 {settings.email.to}
          </div>
        )}

        {scan?.result && (
          <div className={`scan-summary ${scan.result.status}`}>
            上次扫描（{scan.result.trigger}，{scan.result.status}）：
            采集 {scan.result.scanned} · 新增事件 {scan.result.new_events}
            {" · "}新告警 {scan.result.alerts_created}
            {scan.result.data_gaps.length > 0 &&
              <span className="muted-text"> · {scan.result.data_gaps[0]}</span>}
            {scan.result.status === "partial" &&
              <span className="muted-text"> · 部分数据源失败，已降级</span>}
          </div>
        )}

        {importOpen && (
          <div className="import-form">
            <input
              placeholder="事件标题（必填，≤200字）"
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
            />
            <select value={form.type}
              onChange={(e) => setForm({ ...form, type: e.target.value })}>
              <option value="policy">政策事件</option>
              <option value="sector">板块事件</option>
              <option value="stock">个股事件</option>
              <option value="calendar">投资日历</option>
            </select>
            <textarea
              placeholder="事件内容/来源链接（可选）"
              rows={2}
              value={form.content}
              onChange={(e) => setForm({ ...form, content: e.target.value })}
            />
            <div className="import-actions">
              <button onClick={submitImport}
                disabled={!form.title.trim()}>导入事件</button>
              {importMsg && <span className="muted-text">{importMsg}</span>}
            </div>
          </div>
        )}

        <div className="alerts-filters">
          <select value={type} onChange={(e) => setType(e.target.value)}>
            <option value="">全部方向</option>
            <option value="risk">利空</option>
            <option value="opportunity">利多</option>
          </select>
          <select value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="">全部级别</option>
            <option value="high">高</option>
            <option value="medium">中</option>
            <option value="low">低</option>
          </select>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">未读+已读</option>
            <option value="active">仅未读</option>
            <option value="read">仅已读</option>
            <option value="expired">仅过期</option>
          </select>
          <span className="muted-text ws-status">
            {loading ? "加载中…" : `共 ${alerts.length} 条`}
          </span>
        </div>

        {error && <div className="error-box">{error}</div>}

        <ul className="alert-items">
          {alerts.map((a) => (
            <li
              key={a.alert_id}
              className={`alert-item level-${a.alert_level} ${a.status}`}
              onClick={() => void selectAlert(a)}
            >
              <div className="alert-item-head">
                <span className={`badge alert-tag-${a.alert_type}`}>
                  {TYPE_LABEL[a.alert_type]}·{LEVEL_LABEL[a.alert_level]}
                </span>
                <span className="alert-item-title">{a.title}</span>
                {a.status === "active" && <span className="unread-dot" />}
              </div>
              <div className="alert-item-meta">
                {/* 来源按假名脱敏后**不给用户看真名**（数据源保密）。
                    管理员响应里才有 `source_name`，那时显示真名便于排障；
                    普通用户显示"来源已隐藏" —— 不把 `src-xxxx` 假名印出来，
                    那会让人以为是个内部编号。 */}
                {a.source_name || "来源已隐藏"}
                {" · "}{a.event_publish_time || a.trigger_time}
                {" · "}利空{Math.round(a.risk_score)}
                /利多{Math.round(a.opportunity_score)}
                /置信{(a.confidence * 100).toFixed(0)}%
              </div>
            </li>
          ))}
          {!loading && alerts.length === 0 && (
            <li className="muted-text empty-tip">暂无符合条件的告警</li>
          )}
        </ul>
        {settings?.disclaimer && (
          <p className="disclaimer alerts-disclaimer">{settings.disclaimer}</p>
        )}
      </div>

      <AlertDetail alert={selected} onClose={() => setSelected(null)}
                   disclaimer={settings?.disclaimer} />
    </div>
  );
}
