import { useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { ArrowUp, Square, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { useStaff } from "./context";
import { MARKET_LABELS } from "./format";
import { Badge, Notice } from "./StaffBits";
import type { StaffChatMessage } from "./useStaffChat";

// Policy answers are untrusted text. Render link labels as text and omit
// remote images so a model response cannot drive navigation or external loads.
const safeMarkdownComponents: Components = {
  a: ({ children }) => <span>{children}</span>,
  img: ({ alt }) => <span>{alt ? `（图像已省略：${alt}）` : "（图像已省略）"}</span>,
};

export function StaffChat({
  messages,
  busy,
  onSend,
  onStop,
  onClose,
}: {
  messages: StaffChatMessage[];
  busy: boolean;
  onSend: (message: string) => Promise<boolean>;
  onStop: () => void;
  onClose: () => void;
}) {
  const staff = useStaff();
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (busy || !draft.trim()) return;
    const value = draft;
    setDraft("");
    void onSend(value).finally(() => endRef.current?.scrollIntoView({ block: "end" }));
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className="staff-chat-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <aside className="staff-chat" role="dialog" aria-modal="true" aria-label="政策问答">
        <header className="staff-chat-head">
          <div>
            <h2>政策问答</h2>
            <p>{MARKET_LABELS[staff.market]} · 品牌 epig · 引用来自真实政策库</p>
          </div>
          <button className="staff-icon-button" type="button" aria-label="关闭政策问答" onClick={onClose}>
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        <Notice tone="info" title="仅用于内部政策查询">
          不会创建、审批或派遣工单；聊天记录只保留在当前页面内存，切换市场或退出后清除。
        </Notice>
        <div className="staff-chat-messages" aria-live="polite">
          {messages.length === 0 ? (
            <div className="staff-chat-empty">
              <p>例如：这个市场退货政策要求哪些材料？</p>
              <p>回答只基于当前市场、当前日期的真实检索证据。</p>
            </div>
          ) : null}
          {messages.map((message) => (
            <div className={`staff-chat-message ${message.role}`} key={message.id}>
              {message.role === "user" ? (
                <div className="staff-chat-bubble">{message.content}</div>
              ) : (
                <div className="staff-chat-answer">
                  {message.content ? (
                    <div className="markdown staff-chat-markdown">
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={safeMarkdownComponents}
                      >
                        {message.content}
                      </ReactMarkdown>
                    </div>
                  ) : message.state === "streaming" ? (
                    <p className="staff-muted">正在检索并生成回答…</p>
                  ) : null}
                  {message.citations && message.citations.length > 0 ? (
                    <div className="staff-chat-citations" aria-label="回答来源">
                      <span>来源</span>
                      {message.citations.map((citation) => (
                        <span title={`文档 ${citation.document_id} · 片段 ${citation.chunk_id}`} key={citation.chunk_id}>
                          <Badge tone="info">
                            {citation.title}{citation.section ? ` · ${citation.section}` : ""}
                          </Badge>
                        </span>
                      ))}
                    </div>
                  ) : null}
                  {message.state === "stopped" ? <p className="staff-muted">生成已停止</p> : null}
                </div>
              )}
            </div>
          ))}
          <div ref={endRef} />
        </div>
        <form className="staff-chat-composer" onSubmit={submit}>
          <label className="staff-field">
            <span className="staff-field-label">询问当前市场政策</span>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              maxLength={4000}
              rows={2}
              placeholder="Shift + Enter 换行"
            />
          </label>
          {busy ? (
            <button className="staff-button" type="button" onClick={onStop}>
              <Square size={14} aria-hidden="true" />停止生成
            </button>
          ) : (
            <button className="staff-button staff-button-primary" type="submit" disabled={!draft.trim()}>
              <ArrowUp size={15} aria-hidden="true" />发送
            </button>
          )}
        </form>
      </aside>
    </div>
  );
}
