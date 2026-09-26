import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ConceptSuggestion } from "../api";

/**
 * 关联板块输入框（**带联想**）。
 *
 * ## 为什么需要它
 *
 * 原来是裸 `<input>`，要用户自己手打 `PCB概念,PET铜箔` —— 前提是他先知道这只票
 * 属于什么概念。而板块情绪、板块涨幅排行两个维度**全靠这个绑定**，不填就等于丢分。
 *
 * ## 设计要点
 *
 * - **联想**：打字即查（防抖 220ms），数据来自概念库（Tushare 同花顺指数名录，
 *   已剔除"同花顺全A""百元股"这类市场级/量化标签概念）；
 * - **多值**：内部用 `string[]` 维护 chips，对外仍输出逗号分隔字符串
 *   （与 `IntradayTPanel` 原有的 `boardInput` 契约一致，改动面最小）；
 * - **输入法安全**：组字期间不发查询，上屏后才查（否则拼音串会打出满屏无关联想）；
 * - **不覆盖用户输入**：`autoValue` 只在本框为空时填入（用户手打过的内容优先级最高）。
 */
export function BoardPicker({
  value, onChange, autoValue = [], disabled = false, width = 220,
  placeholder = "关联板块(可选) 如 PCB概念", listId,
}: {
  value: string;
  onChange: (next: string) => void;
  /**
   * 从个股推导出的"最相关概念"（**按相关性降序，默认取前 3 个一起关联**）。
   * 只在本框为空时自动填入；用户手打或删过之后不再干预。
   */
  autoValue?: string[];
  disabled?: boolean;
  width?: number;
  placeholder?: string;
  /**
   * 原生 `<datalist>` 的 id（用户口径 2026-09-23）：调用方把**用户以前配过的板块名**
   * 挂在那个 datalist 上，浏览器就会在本框给历史配置做自动补全。
   *
   * 为什么不并进下面那个自研下拉：那个查的是**概念库**（Tushare 名录，几万条），
   * 语料是"全集"；这里要提示的是"**你用过什么**"，语料是"用户自己的历史"，
   * 两者来源与刷新时机都不同。用原生 datalist 搭在同一个 input 上，
   * 互不干扰、也不用再写一套下拉。
   */
  listId?: string;
}) {
  const [keyword, setKeyword] = useState("");
  const [options, setOptions] = useState<ConceptSuggestion[]>([]);
  const [open, setOpen] = useState(false);
  const [composing, setComposing] = useState(false);
  const [loading, setLoading] = useState(false);
  const timerRef = useRef<number | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);
  /** 请求序号：丢弃过期响应（与 `StockPicker` 同一个竞态，见那边的说明） */
  const querySeq = useRef(0);
  // 用户是否手动改过：改过就不再自动填（尊重手输）
  const touchedRef = useRef(false);

  const boards = useMemo(
    () => value.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean),
    [value]);

  const commit = useCallback((next: string[]) => {
    onChange([...new Set(next)].join(","));
  }, [onChange]);

  const query = useCallback((text: string) => {
    if (timerRef.current) window.clearTimeout(timerRef.current);
    if (!text.trim()) {
      querySeq.current += 1;      // 清空要作废在途请求，否则旧结果会把空列表填回来
      setOptions([]);
      setOpen(false);
      return;
    }
    const seq = ++querySeq.current;
    timerRef.current = window.setTimeout(async () => {
      setLoading(true);
      try {
        const data = await api.conceptSuggest(text.trim(), 10);
        if (seq !== querySeq.current) return;    // 过期响应：丢弃（与 StockPicker 同一竞态）
        // 已经在 chips 里的不再重复建议
        setOptions(data.items.filter((item) => !boards.includes(item.name)));
        setOpen(true);
      } catch {
        if (seq === querySeq.current) setOptions([]);
      } finally {
        if (seq === querySeq.current) setLoading(false);
      }
    }, 220);
  }, [boards]);

  useEffect(() => () => {
    if (timerRef.current) window.clearTimeout(timerRef.current);
  }, []);

  // 自动关联最相关概念：只在本框为空、且用户没手动改过时生效。
  // **默认填前 3 个**（用户口径 2026-09-17）：只填 1 个覆盖面太窄 ——
  // 板块情绪/板块涨幅排行两个维度靠它绑定，多关联 2 个能让打分更稳；
  // 但也不能全填（相关性第 4 名之后基本是噪声，反而稀释权重）。
  useEffect(() => {
    if (disabled) return;
    if (touchedRef.current) return;
    if (boards.length > 0) return;
    const names = (autoValue ?? []).filter(Boolean).slice(0, 3);
    if (names.length === 0) return;
    commit(names);
  }, [autoValue, boards.length, commit, disabled]);

  useEffect(() => {
    const onClickOutside = (event: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  const add = (name: string) => {
    touchedRef.current = true;
    commit([...boards, name]);
    setKeyword("");
    setOptions([]);
    setOpen(false);
  };

  const remove = (name: string) => {
    touchedRef.current = true;
    commit(boards.filter((item) => item !== name));
  };

  return (
    <div className="board-picker" ref={boxRef} style={{ width }}>
      <div className="board-picker-row">
        {boards.map((name) => (
          <span key={name} className="board-chip">
            {name}
            <button className="chip-x" type="button" disabled={disabled}
                    onClick={() => remove(name)} title="移除该关联板块">×</button>
          </span>
        ))}
        <input
          className="board-input"
          value={keyword}
          disabled={disabled}
          spellCheck={false}
          list={listId}
          placeholder={boards.length ? "继续添加板块…" : placeholder}
          title="支持概念名联想（数据来自 Tushare 同花顺指数名录）；关联板块决定「板块情绪」与「板块涨幅排行」两个维度是否计分"
          onChange={(event) => {
            const next = event.target.value;
            setKeyword(next);
            if (composing) return;
            query(next);
          }}
          onCompositionStart={() => setComposing(true)}
          onCompositionEnd={(event) => {
            setComposing(false);
            const next = (event.target as HTMLInputElement).value;
            setKeyword(next);
            query(next);
          }}
          onFocus={() => { if (options.length) setOpen(true); }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && keyword.trim()) {
              event.preventDefault();
              add(options[0]?.name ?? keyword.trim());
            } else if (event.key === "Backspace" && !keyword && boards.length) {
              remove(boards[boards.length - 1]);
            } else if (event.key === "Escape") {
              setOpen(false);
            }
          }}
        />
      </div>
      {open && (options.length > 0 || loading) && (
        <ul className="suggest-list board-suggest">
          {loading && options.length === 0 && <li className="muted-text">查询中…</li>}
          {options.map((item) => (
            <li key={item.name} onMouseDown={(event) => {
              event.preventDefault();
              add(item.name);
            }}>
              <span className="suggest-name">{item.name}</span>
              <span className="mono muted-text suggest-py">{item.members} 只</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default BoardPicker;
