import { useCallback, useEffect, useRef, useState } from "react";
import { useStaff } from "./context";
import { errorMessage } from "./format";
import { AttachmentUrlStore } from "./guard";

export interface AttachmentPreview {
  state: "loading" | "ready" | "error";
  url?: string;
  mediaType?: string;
  message?: string;
}

export function useAttachmentUrls(caseId: number | null) {
  const staff = useStaff();
  const storeRef = useRef<AttachmentUrlStore | null>(null);
  if (storeRef.current === null) storeRef.current = new AttachmentUrlStore();
  const [previews, setPreviews] = useState<Record<string, AttachmentPreview>>({});
  const caseRef = useRef<number | null>(caseId);
  caseRef.current = caseId;
  const aliveRef = useRef(true);

  useEffect(() => {
    setPreviews({});
    return () => {
      storeRef.current?.revokeAll();
    };
  }, [caseId]);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      // A late authenticated download must not create an orphan object URL
      // after the case/identity scope has already been cleaned up.
      aliveRef.current = false;
      storeRef.current?.revokeAll();
    };
  }, []);

  const load = useCallback(async (attachmentId: string) => {
    const store = storeRef.current;
    if (!store) return;
    const currentCase = caseId;
    if (currentCase === null) return;
    if (store.has(attachmentId)) {
      const url = store.get(attachmentId);
      if (url) {
        setPreviews((previous) => ({
          ...previous,
          [attachmentId]: {
            state: "ready",
            url,
            mediaType: previous[attachmentId]?.mediaType,
          },
        }));
      }
      return;
    }
    const generation = staff.generation;
    setPreviews((previous) => ({ ...previous, [attachmentId]: { state: "loading" } }));
    const result = await staff.run((api, signal) =>
      api.getAttachment(staff.market, attachmentId, signal),
    );
    if (!result.ok) {
      if (result.stale || !aliveRef.current || !staff.isCurrent(generation) || caseRef.current !== currentCase) return;
      setPreviews((previous) => ({
        ...previous,
        [attachmentId]: { state: "error", message: errorMessage(result.error) },
      }));
      return;
    }
    if (!aliveRef.current || !staff.isCurrent(generation) || caseRef.current !== currentCase) return;
    const url = store.put(attachmentId, result.value.blob);
    setPreviews((previous) => ({
      ...previous,
      [attachmentId]: {
        state: "ready",
        url,
        mediaType: result.value.mediaType,
      },
    }));
  }, [caseId, staff]);

  const revokeAll = useCallback(() => {
    storeRef.current?.revokeAll();
    setPreviews({});
  }, []);

  return { previews, load, revokeAll };
}
