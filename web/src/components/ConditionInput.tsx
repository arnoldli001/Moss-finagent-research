import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type QuantHelp } from "../api";

/**
 * 条件输入框：带**变量/函数自动联想**。
 *
 * 候选词来自后端说明书（因子键 + 面板字段 + DSL 函数），不是前端硬编码的列表 ——
 * 否则加了因子、改了函数签名，联想出来的还是旧词表，比没有联想更误导。
 *
 * 交互细节（都是实测出来的必要行为）：
 *  - 只在光标处**词内**触发，且至少有 1 个字符前缀（避免刚聚焦就弹一屏）；
 *  - `↑ ↓` 选择、`Enter/Tab` 采纳、`Esc` 关闭；
 *  - 采纳函数时自动补 `(`，并把光标放进括号里（写 `MA(` 之后直接写窗口天数）；
 *  - 点击外部关闭。
 */

type Suggestion = {
  name: string;
  kind: string;
  hint: string;
  insert: string;
  caretOffset: number;   // 采纳后光标相对插入文本末尾的偏移
};

const KEYWORDS = ["AND", "OR", "NOT", "BETWEEN", "IN", "TRUE", "FALSE"];

export function ConditionInput({
  value, onChange, placeholder, rows = 2, disabled = false, topic = "single",
  onOpenHelp,
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  rows?: number;
  disabled?: boolean;
  topic?: "single" | "factors";
  onOpenHelp?: () => void;
}) {
  const [vocabulary, setVocabulary] = useState<Suggestion[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [token, setToken] = useState("");
  const areaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    let alive = true;
    api.quantHelp(topic)
      .then((help: QuantHelp) => {
        if (!alive) return;
        setVocabulary(buildVocabulary(help));
      })
      .catch(() => { /* 联想不可用不影响手写条件 */ });
    return () => { alive = false; };
  }, [topic]);

  const matches = useMemo(() => {
    if (!token) return [];
    const prefix = token.toLowerCase();
    return vocabulary
      .filter((item) => item.name.toLowerCase().startsWith(prefix)
        && item.name.toLowerCase() !== prefix)
      .sort((a, b) => {
        // 完全前缀匹配优先，其次短的优先（`MA` 比 `MEAN_TS` 更常用）
        const length = a.name.length - b.name.length;
        return length !== 0 ? length : a.name.localeCompare(b.name);
      })
      .slice(0, 12);
  }, [token, vocabulary]);

  const currentToken = useCallback((text: string, caret: number) => {
    const head = text.slice(0, caret);
    const match = head.match(/[A-Za-z_][A-Za-z0-9_]*$/);
    return match ? match[0] : "";
  }, []);

  const handleChange = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    const next = event.target.value;
    onChange(next);
    const caret = event.target.selectionStart ?? next.length;
    const word = currentToken(next, caret);
    setToken(word);
    setActive(0);
    setOpen(word.length >= 1 && /\D/.test(word[0]));
  };

  const accept = (item: Suggestion) => {
    const area = areaRef.current;
    if (!area) return;
    const caret = area.selectionStart ?? value.length;
    const head = value.slice(0, caret);
    const tail = value.slice(caret);
    const replaced = head.replace(/[A-Za-z_][A-Za-z0-9_]*$/, "");
    const next = `${replaced}${item.insert}${tail}`;
    onChange(next);
    const position = replaced.length + item.insert.length + item.caretOffset;
    setOpen(false);
    setToken("");
    window.requestAnimationFrame(() => {
      area.focus();
      area.setSelectionRange(position, position);
    });
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (!open || !matches.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((index) => (index + 1) % matches.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => (index - 1 + matches.length) % matches.length);
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      accept(matches[active]);
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  };

  return (
    <div className="condition-input">
      <textarea
        ref={areaRef}
        value={value}
        rows={rows}
        disabled={disabled}
        placeholder={placeholder}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onBlur={() => window.setTimeout(() => setOpen(false), 150)}
        spellCheck={false}
      />
      <div className="condition-tools">
        <span className="muted-text" style={{ fontSize: 11 }}>
          输入 2 个以上字母自动联想（↑↓ 选择，Enter/Tab 采纳）
        </span>
        {onOpenHelp && (
          <button className="btn-ghost tiny" type="button" onClick={onOpenHelp}
                  title="查看全部函数、字段、因子与参数说明">
            ? 语法手册
          </button>
        )}
      </div>
      {open && matches.length > 0 && (
        <ul className="suggest-list">
          {matches.map((item, index) => (
            <li key={item.name}
                className={index === active ? "active" : ""}
                onMouseDown={(event) => {
                  event.preventDefault();
                  accept(item);
                }}>
              <span className="mono suggest-name">{item.name}</span>
              <span className="badge badge-muted">{item.kind}</span>
              <span className="muted-text suggest-hint">{item.hint}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function buildVocabulary(help: QuantHelp): Suggestion[] {
  const words: Suggestion[] = [];
  for (const group of help.factors ?? []) {
    for (const factor of group.factors) {
      words.push({
        name: factor.key, kind: group.category,
        hint: factor.label,
        insert: factor.key, caretOffset: 0,
      });
    }
  }
  for (const group of help.panel_fields ?? []) {
    for (const column of group.columns) {
      words.push({
        name: column, kind: "字段", hint: group.group,
        insert: column, caretOffset: 0,
      });
    }
  }
  for (const fn of help.dsl?.functions ?? []) {
    words.push({
      name: fn.name, kind: `${fn.kind}函数`,
      hint: fn.example,
      // 函数自动补左括号，并把光标放在括号后（例：MA( → 直接写窗口天数）
      insert: `${fn.name}(`, caretOffset: 0,
    });
  }
  for (const keyword of KEYWORDS) {
    words.push({ name: keyword, kind: "关键字", hint: "", insert: keyword,
                 caretOffset: 0 });
  }
  // 去重（因子键与面板字段可能重名，如 roe）
  const seen = new Set<string>();
  return words.filter((item) => {
    if (seen.has(item.name)) return false;
    seen.add(item.name);
    return true;
  });
}
