import { useCallback, useEffect, useRef, useState } from "react";
import { isSessionEndingError } from "./api";
import { useStaff } from "./context";
import { errorMessage } from "./format";
import type { StaffChatAnswer } from "./types";

export interface StaffChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  state: "streaming" | "complete" | "error" | "stopped";
  citations?: StaffChatAnswer["citations"];
}

const MAX_MESSAGE_LENGTH = 4000;

export function useStaffChat() {
  const staff = useStaff();
  const [messages, setMessages] = useState<StaffChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const sessionIdRef = useRef<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const busyRef = useRef(false);
  const unansweredRef = useRef(false);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const stop = useCallback(() => {
    controllerRef.current?.abort();
  }, []);

  const send = useCallback(async (raw: string) => {
    const text = raw.trim();
    if (!text || text.length > MAX_MESSAGE_LENGTH || busyRef.current) return false;
    const generation = staff.generation;
    const controller = new AbortController();
    controllerRef.current = controller;
    busyRef.current = true;
    setBusy(true);
    const userMessage: StaffChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: text,
      state: "complete",
    };
    const assistantId = crypto.randomUUID();
    unansweredRef.current = true;
    setMessages((previous) => [
      ...previous,
      userMessage,
      { id: assistantId, role: "assistant", content: "", state: "streaming" },
    ]);

    const payload = {
      message: text,
      user_id: staff.identity.staff_id,
      brand: "epig" as const,
      market: staff.market,
      ...(sessionIdRef.current ? { session_id: sessionIdRef.current } : {}),
    };

    try {
      await staff.api.streamChat(
        payload,
        (event) => {
          if (!staff.isCurrent(generation) || controller.signal.aborted) return;
          if (event.event === "token") {
            setMessages((previous) => previous.map((message) =>
              message.id === assistantId
                ? { ...message, content: message.content + event.text }
                : message,
            ));
          } else if (event.event === "answer") {
            unansweredRef.current = false;
            if (event.data.session_id) sessionIdRef.current = event.data.session_id;
            setMessages((previous) => previous.map((message) =>
              message.id === assistantId
                ? {
                    ...message,
                    content: event.data.answer,
                    state: "complete",
                    citations: event.data.citations,
                  }
                : message,
            ));
          }
        },
        controller.signal,
      );
      if (!staff.isCurrent(generation)) return false;
      if (unansweredRef.current) throw new Error("连接已结束，但未收到最终回答。");
      return true;
    } catch (error) {
      if (!staff.isCurrent(generation)) return false;
      const stopped = controller.signal.aborted;
      if (!stopped && isSessionEndingError(error)) {
        staff.reportSessionEnding(error, generation);
        return false;
      }
      setMessages((previous) => previous.map((message) =>
        message.id === assistantId
          ? {
              ...message,
              content: stopped
                ? message.content || "已停止生成。"
                : errorMessage(error),
              state: stopped ? "stopped" : "error",
            }
          : message,
      ));
      return false;
    } finally {
      if (staff.isCurrent(generation)) {
        busyRef.current = false;
        setBusy(false);
      }
      if (controllerRef.current === controller) controllerRef.current = null;
    }
  }, [staff]);

  return { messages, busy, send, stop };
}
