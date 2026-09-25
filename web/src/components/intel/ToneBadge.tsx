/**
 * 原文倾向标签（`ToneBadge`）—— 单独一个文件，因为它承载的合规约束
 * 比情报流其它部分加起来还多。
 *
 * ## 三个必须写进界面的限定（否则就是越线）
 *
 * 1. 标签文字是「**原文倾向**」而不是「倾向」—— 前者说明这是**第三方
 *    原文的语气**，后者读起来像平台观点。证监会公告〔2012〕40号明确禁止
 *    "以不同措辞表达"投资分析意见，所以这个前缀不是装饰。
 * 2. `未定` 是**一等公民**：规则层与模型层判定不一致时它就是正确答案。
 *    界面显示"不做倾向归类"，而不是硬塞一个可能错的标签。
 * 3. **永远**给出依据（`phrases`，逐字来自原文）。说不出理由的标签，
 *    用户只能选择信或不信，而两者都不合适。
 *
 * ## 为什么没有 `tone` 字段时要说"尚未抽取"
 *
 * 抽取是 2 小时一次的定时任务。新条目在下一批抽到之前**确实没有**倾向 ——
 * 显示"尚未抽取"是如实；显示"中性"是编造（那是"抽过了，判定为中性"）。
 */

import { useState } from "react";
import { IntelTone } from "../../intelApi";

export default function ToneBadge({ tone }: { tone?: IntelTone }) {
  const [open, setOpen] = useState(false);

  if (!tone) {
    return <span className="tone-pending muted-text">倾向尚未抽取</span>;
  }

  const cls = !tone.has_tone ? "unknown"
    : tone.tone === "偏多" ? "bull"
      : tone.tone === "偏空" ? "bear" : "neutral";
  const hasEvidence = tone.phrases.length > 0 || tone.codes.length > 0;

  return (
    <div className={`tone${open ? " open" : ""}`}>
      <button
        className={`tone-tag ${cls}`}
        aria-expanded={open}
        title={tone.explain}
        onClick={(e) => {
          // 卡片整体也可点（展开摘要），别让这个点击冒泡上去
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        原文倾向 <b>{tone.tone}</b>
        {/* 置信度**只在服务端给了值时才显示** —— 不一致时为 null，
            那是"有幻觉风险，不显示数值"的落地 */}
        {tone.confidence !== null && (
          <span className="tone-conf">
            {Math.round(tone.confidence * 100)}%
          </span>
        )}
      </button>

      {open && (
        <div className="tone-detail">
          <div className="tone-explain muted-text">{tone.explain}</div>
          {tone.phrases.length > 0 && (
            <div className="tone-evidence">
              <span className="muted-text">原文依据</span>
              {tone.phrases.map((p, i) => (
                <span className="tone-phrase" key={i}>{p}</span>
              ))}
            </div>
          )}
          {tone.codes.length > 0 && (
            <div className="tone-evidence">
              <span className="muted-text">原文提到</span>
              {tone.codes.map((c) => (
                <span className="tone-phrase code" key={c}>{c}</span>
              ))}
            </div>
          )}
          {tone.has_tone && tone.confidence === null && (
            <div className="muted-text tone-note">
              未给置信度：只有一层（词表或语义）有判断，不足以自称有把握
            </div>
          )}
          {!hasEvidence && (
            <div className="muted-text tone-note">
              无逐字依据可列（判定来自词表计数或语义归类）
            </div>
          )}
          <div className="muted-text tone-note">
            这是对第三方原文语气的归类，不是平台判断，也不构成投资建议
          </div>
        </div>
      )}
    </div>
  );
}
