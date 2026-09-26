import { Alert } from "../api";

type Props = {
  alert: Alert | null;
  onClose: () => void;
  /** 免责声明文案。由调用方传入（设置接口的 `disclaimer`）。
   *
   * 为什么不再是 `alert.disclaimer`：那条 91 字节文案在列表里**每条都重复
   * 一遍**（95 条 = 8,645 B，占整个载荷 7.7%），而隧道实测只有 ~51 KB/s
   * —— 重复 95 遍的同一句话要从用户的等待时间里扣掉好几秒。
   * 所以服务端在列表投影里把它去掉了（`alert_to_public(for_list=True)`），
   * 改由**响应级**的 `settings.disclaimer` 传一次。
   *
   * 兜底给一句常量：拿不到设置时也不能让合规声明消失。 */
  disclaimer?: string;
};

/** 拿不到设置接口时的兜底文案（与后端 `DISCLAIMER` 同义，不含新信息）。 */
const FALLBACK_DISCLAIMER =
  "事件告警由AI基于公开信息自动生成，仅供参考，不构成投资建议。";

const IMPACT_LABEL: Record<string, string> = {
  positive: "受益", negative: "受损", mixed: "影响分化",
};

/** 告警详情：评分/置信度/受影响个股表/影响路径/溯源外链/免责声明。 */
export default function AlertDetail({ alert, onClose, disclaimer }: Props) {
  if (!alert) {
    return (
      <div className="panel alert-detail empty">
        <h2>告警详情</h2>
        <p className="muted-text">选择左侧告警查看评分、受益/受损个股与原文溯源。</p>
      </div>
    );
  }
  const riskType = alert.alert_type === "risk";
  return (
    <div className="panel alert-detail">
      <div className="detail-head">
        <h2 title={alert.title}>{alert.title}</h2>
        <button className="detail-close" onClick={onClose}>×</button>
      </div>
      <div className="detail-badges">
        <span className={`badge alert-tag-${alert.alert_type}`}>
          {/* 展示词：利空 / 利多（底层枚举仍是 risk/opportunity）。
              理由见 `AlertsPanel.TYPE_LABEL` 的说明。 */}
          {riskType ? "利空" : "利多"}·{alert.alert_level}
        </span>
        <span className={`badge ${alert.status === "active"
          ? "conf-low" : "conf-medium"}`}>
          {alert.status === "active" ? "未读" : alert.status === "read" ? "已读" : "已过期"}
        </span>
      </div>

      {alert.description && <p className="detail-summary">{alert.description}</p>}

      <div className="score-row">
        <div className="score-cell risk">
          <label>利空分</label>
          <strong>{Math.round(alert.risk_score)}</strong>
        </div>
        <div className="score-cell opp">
          <label>利多分</label>
          <strong>{Math.round(alert.opportunity_score)}</strong>
        </div>
        <div className="score-cell conf">
          <label>置信度</label>
          <strong>{(alert.confidence * 100).toFixed(0)}%</strong>
        </div>
      </div>

      {alert.affected_industries.length > 0 && (
        <h3>相关行业</h3>
      )}
      {alert.affected_industries.length > 0 && (
        <div className="industry-tags">
          {alert.affected_industries.map((ind) => (
            <span key={ind} className="industry-tag">{ind}</span>
          ))}
        </div>
      )}

      {alert.affected_stocks.length > 0 && (
        <>
          <h3>受影响个股</h3>
          <table className="audit-table stock-table">
            <thead>
              <tr><th>代码</th><th>名称</th><th>影响</th><th>原因</th></tr>
            </thead>
            <tbody>
              {alert.affected_stocks.map((s, i) => (
                <tr key={`${s.code}-${i}`}>
                  <td className="mono">{s.code || "待核"}</td>
                  <td>{s.name}</td>
                  <td className={`impact-${s.impact}`}>
                    {IMPACT_LABEL[s.impact] ?? s.impact}
                  </td>
                  <td className="muted-text">{s.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {alert.impact_path && (
        <>
          <h3>影响路径</h3>
          <p className="impact-path">{alert.impact_path}</p>
        </>
      )}

      <h3>溯源</h3>
      <div className="provenance">
        {/* ⚠️ 数据源保密：普通用户的响应里**没有** `source_name` /
            `source_url`（后端在契约层就不构造它们），所以这里只有管理员
            才看得到真名与原文链接。
            原来这里直接 `<a href={alert.source_url}>查看原文</a>`，
            等于把"东方财富快讯的某篇文章"作为可点链接交给任何登录用户 ——
            点一下就知道了我们的渠道。 */}
        <div>来源：{alert.source_name || "已隐藏（渠道信息不对用户开放）"}</div>
        <div>事件时间：{alert.event_publish_time || "未知"}</div>
        <div>触发时间：{alert.trigger_time}</div>
        {alert.source_url && (
          // 仅管理员走到这里（普通用户拿不到该字段）
          <a href={alert.source_url} target="_blank" rel="noreferrer">
            查看原文 ↗（管理员）
          </a>
        )}
      </div>

      <p className="disclaimer">{disclaimer || FALLBACK_DISCLAIMER}</p>
    </div>
  );
}
