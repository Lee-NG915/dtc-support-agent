import { useEffect, useState } from "react";
import { MessageCircle, X } from "lucide-react";
import { CasePanel } from "./CasePanel";
import { QueuePanel } from "./QueuePanel";
import { EmptyBlock } from "./StaffBits";
import { StaffChat } from "./StaffChat";
import { useStaffChat } from "./useStaffChat";

export function StaffWorkbench() {
  const [selectedCaseId, setSelectedCaseId] = useState<number | null>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const chat = useStaffChat();

  useEffect(() => {
    if (!chatOpen) return;
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        chat.stop();
        setChatOpen(false);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [chatOpen]);

  return (
    <div className="staff-workbench">
      <div className="staff-workbench-grid">
        <QueuePanel
          selectedCaseId={selectedCaseId}
          onSelect={(caseId) => setSelectedCaseId(caseId)}
        />
        <div className="staff-detail-column">
          {selectedCaseId === null ? (
            <div className="staff-detail-placeholder">
              <EmptyBlock title="从左侧队列选择一个诉求">
                详情会展示消费者诉求、受保护附件、订单与物流、真实政策证据、草稿、冻结审批快照、执行状态和真实工单读取结果。
              </EmptyBlock>
            </div>
          ) : (
            <CasePanel key={selectedCaseId} caseId={selectedCaseId} />
          )}
        </div>
      </div>

      <button
        className="staff-chat-fab"
        type="button"
        aria-expanded={chatOpen}
        onClick={() => {
          if (chatOpen) chat.stop();
          setChatOpen((value) => !value);
        }}
      >
        {chatOpen ? <X size={16} aria-hidden="true" /> : <MessageCircle size={16} aria-hidden="true" />}
        {chatOpen ? "关闭政策问答" : "政策问答"}
      </button>

      {chatOpen ? (
        <StaffChat
          messages={chat.messages}
          busy={chat.busy}
          onSend={chat.send}
          onStop={chat.stop}
          onClose={() => {
            chat.stop();
            setChatOpen(false);
          }}
        />
      ) : null}
    </div>
  );
}
