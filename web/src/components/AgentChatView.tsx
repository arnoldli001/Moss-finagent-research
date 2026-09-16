import { useMemo } from "react";
import { AgentMessage } from "../api";
import { agentLabel } from "../agentMeta";

type Props = { messages: AgentMessage[] };

type Pair = { question: AgentMessage; answer?: AgentMessage };

/**
 * 展示Agent间多轮对话（A2A协作过程）。
 * - 按 message_id 幂等去重（稳定标识，不做文本去重，防止轮询合并时重复渲染）
 * - 按 reply_to 把"提问-回答"配对成一轮协作，避免问答散列造成"重复提问"观感
 */
export default function AgentChatView({ messages }: Props) {
  const pairs = useMemo<Pair[]>(() => {
    const seen = new Set<string>();
    const deduped: AgentMessage[] = [];
    (messages ?? []).forEach((m, i) => {
      const id = m.message_id || `__idx_${i}`;
      if (seen.has(id)) return;
      seen.add(id);
      deduped.push(m);
    });

    const result: Pair[] = [];
    const answerByReply = new Map<string, AgentMessage>();
    deduped.forEach((m) => {
      if (m.message_type === "answer" && m.reply_to) {
        answerByReply.set(m.reply_to, m);
      }
    });

    const pairedAnswerIds = new Set<string>();
    deduped.forEach((m) => {
      if (m.message_type !== "question") return;
      const answer = answerByReply.get(m.message_id);
      if (answer) pairedAnswerIds.add(answer.message_id);
      result.push({ question: m, answer });
    });
    // 无对应提问的回答（兜底，正常不会出现）
    deduped.forEach((m) => {
      if (m.message_type === "answer" && !pairedAnswerIds.has(m.message_id)) {
        result.push({ question: m });
      }
    });
    return result;
  }, [messages]);

  if (pairs.length === 0) return null;

  return (
    <div className="agent-chat">
      <h3>Agent 协作对话（A2A · {pairs.length} 轮追问）</h3>
      <div className="chat-list">
        {pairs.map(({ question, answer }, i) => {
          const isOrphan = question.message_type === "answer";
          return (
            <div className="chat-round" key={question.message_id || i}>
              <div className="chat-round-head">
                <span className="chat-round-no">#{i + 1}</span>
                {!isOrphan && (
                  <>
                    <span className="chat-sender" title={question.sender}>
                      {agentLabel(question.sender)}
                    </span>
                    <span className="chat-arrow">→</span>
                    <span className="chat-receiver" title={question.receiver}>
                      {agentLabel(question.receiver)}
                    </span>
                  </>
                )}
              </div>
              {!isOrphan && (
                <div className="chat-msg chat-q">
                  <div className="chat-role">提问</div>
                  <div className="chat-content">{question.content}</div>
                </div>
              )}
              {answer && (
                <div className="chat-msg chat-a">
                  <div className="chat-meta">
                    <span className="chat-sender" title={answer.sender}>
                      {agentLabel(answer.sender)}
                    </span>
                    <span className="chat-role">回答</span>
                  </div>
                  <div className="chat-content">{answer.content}</div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
