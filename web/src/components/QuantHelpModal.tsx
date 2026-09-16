import { useEffect, useState } from "react";
import { api, type QuantHelp } from "../api";

/**
 * 操作说明书弹窗（内容由后端从代码生成）。
 *
 * 为什么内容放在后端而不是写死在前端：因子清单、DSL 函数、参数默认值
 * 都会随代码变化，手写在前端的说明一定会过期 —— 而过期的说明书比没有更糟，
 * 因为用户会照着它写条件。后端 `help_content.py` 直接从因子注册表、
 * 函数集合常量、dataclass 字段读出这些内容。
 */
export function QuantHelpModal({
  topic, onClose,
}: { topic: "single" | "factors"; onClose: () => void }) {
  const [help, setHelp] = useState<QuantHelp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<string>("guide");

  useEffect(() => {
    let alive = true;
    api.quantHelp(topic)
      .then((data) => { if (alive) setHelp(data); })
      .catch((exc) => {
        if (alive) setError(exc instanceof Error ? exc.message : String(exc));
      });
    return () => { alive = false; };
  }, [topic]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const tabs: { key: string; label: string }[] = [
    { key: "guide", label: "使用说明" },
    { key: "dsl", label: "条件语法" },
    ...(help?.panel_fields?.length ? [{ key: "fields", label: "可用字段" }] : []),
    ...(help?.factors?.length ? [{ key: "factors", label: "35 因子" }] : []),
    ...(help?.params?.length ? [{ key: "params", label: "参数表" }] : []),
    ...(help?.troubleshooting?.length
      ? [{ key: "trouble", label: "排查" }] : []),
  ];

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="help-modal" onClick={(e) => e.stopPropagation()}>
        <div className="help-head">
          <h2>{help?.title ?? "使用说明书"}</h2>
          <button className="btn-ghost tiny" onClick={onClose}>关闭 (Esc)</button>
        </div>
        {error && <div className="error-text">说明书加载失败：{error}</div>}
        {!help && !error && <p className="muted-text">加载中…</p>}
        {help && (
          <>
            <nav className="help-tabs">
              {tabs.map((item) => (
                <button key={item.key}
                        className={tab === item.key ? "mode-btn active" : "mode-btn"}
                        onClick={() => setTab(item.key)}>
                  {item.label}
                </button>
              ))}
            </nav>
            <div className="help-body">
              {tab === "guide" && (
                <>
                  {help.sections.map((section) => (
                    <section key={section.heading}>
                      <h3>{section.heading}</h3>
                      <ul>
                        {section.items.map((item) => (
                          <li key={item}>{renderInline(item)}</li>
                        ))}
                      </ul>
                    </section>
                  ))}
                  {help.limitations?.length ? (
                    <section>
                      <h3>已知限制（如实记录）</h3>
                      <ul>{help.limitations.map((item) => (
                        <li key={item}>{renderInline(item)}</li>
                      ))}</ul>
                    </section>
                  ) : null}
                </>
              )}

              {tab === "dsl" && help.dsl && (
                <>
                  {(["时序", "截面", "逐元素"] as const).map((kind) => {
                    const rows = help.dsl!.functions.filter(
                      (item) => item.kind === kind);
                    if (!rows.length) return null;
                    return (
                      <section key={kind}>
                        <h3>{kind}函数（{rows.length}）</h3>
                        <table className="data-table">
                          <thead>
                            <tr><th>签名</th><th>示例</th></tr>
                          </thead>
                          <tbody>
                            {rows.map((item) => (
                              <tr key={item.name}>
                                <td className="mono">{item.signature}</td>
                                <td className="mono muted-text">{item.example}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </section>
                    );
                  })}
                  <section className="warn-box">
                    <h3>本页**不可用**的函数</h3>
                    <p className="mono">{help.dsl.forbidden.functions.join("、")}</p>
                    <p>{renderInline(help.dsl.forbidden.why)}</p>
                  </section>
                  <section>
                    <h3>运算符</h3>
                    <ul>
                      {Object.entries(help.dsl.operators).map(([key, value]) => (
                        <li key={key}>
                          <b>{key}</b>：{value.join("、")}
                        </li>
                      ))}
                    </ul>
                    <p className="muted-text">{renderInline(help.dsl.pit)}</p>
                  </section>
                </>
              )}

              {tab === "fields" && help.panel_fields && (
                <>
                  {help.panel_fields.map((group) => (
                    <section key={group.group}>
                      <h3>{group.group}（{group.columns.length}）</h3>
                      <p className="mono help-columns">
                        {group.columns.join("  ")}
                      </p>
                      <p className="muted-text">{renderInline(group.note)}</p>
                    </section>
                  ))}
                </>
              )}

              {tab === "factors" && help.factors && (
                <>
                  {help.factors.map((group) => (
                    <section key={group.category}>
                      <h3>{group.category}（{group.count}）</h3>
                      <table className="data-table">
                        <thead>
                          <tr><th>因子键</th><th>名称</th><th>方向</th>
                            <th>公式 / 说明</th></tr>
                        </thead>
                        <tbody>
                          {group.factors.map((item) => (
                            <tr key={item.key}>
                              <td className="mono">{item.key}</td>
                              <td>{item.label}</td>
                              <td className="mono muted-text">
                                {item.direction > 0 ? "越高越好" : "越低越好"}
                              </td>
                              <td className="muted-text">
                                {item.formula || item.note}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </section>
                  ))}
                </>
              )}

              {tab === "params" && help.params && (
                <section>
                  <h3>回测参数（{help.params.length}）</h3>
                  <table className="data-table">
                    <thead>
                      <tr><th>参数</th><th>默认值</th><th>含义</th></tr>
                    </thead>
                    <tbody>
                      {help.params.map((item) => (
                        <tr key={item.name}>
                          <td className="mono">{item.name}</td>
                          <td className="mono muted-text">
                            {typeof item.default === "object"
                              ? JSON.stringify(item.default)
                              : String(item.default)}
                          </td>
                          <td className="muted-text">{renderInline(item.meaning)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </section>
              )}

              {tab === "trouble" && help.troubleshooting && (
                <>
                  {help.troubleshooting.map((item) => (
                    <section key={item.symptom} className="trouble-item">
                      <h3>❗ {item.symptom}</h3>
                      <p><b>原因：</b>{renderInline(item.cause)}</p>
                      <p><b>处理：</b>{renderInline(item.fix)}</p>
                    </section>
                  ))}
                </>
              )}
            </div>
            <p className="muted-text help-foot">{help.disclaimer}</p>
          </>
        )}
      </div>
    </div>
  );
}

/** 极简行内标记渲染：把 **粗体** 与 `代码` 转成元素，其余原样。 */
function renderInline(text: string) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  return parts.map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return <b key={index}>{part.slice(2, -2)}</b>;
    }
    if (part.startsWith("`") && part.endsWith("`")) {
      return <code key={index}>{part.slice(1, -1)}</code>;
    }
    return <span key={index}>{part}</span>;
  });
}
