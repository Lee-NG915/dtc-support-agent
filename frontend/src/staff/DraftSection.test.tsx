// @vitest-environment jsdom
import { act } from "react";
import { describe, expect, it, vi } from "vitest";
import { StaffApiError } from "./api";
import type { StaffContextValue } from "./context";
import { DraftSection } from "./DraftSection";
import type { RunResult } from "./guard";
import {
  click,
  createStaffContext,
  detailFixture,
  draftFixture,
  evidenceFixture,
  pump,
  renderStaff,
  setControlValue,
  settle,
  textOf,
} from "./testSupport";
import type { StaffApi } from "./api";
import type { CaseDetail, DraftCreateInput } from "./types";

function fixedRun(result: RunResult<CaseDetail>): StaffContextValue["run"] {
  return (async () => result) as unknown as StaffContextValue["run"];
}

function apiRun(api: StaffApi): StaffContextValue["run"] {
  return (async (work: (client: StaffApi, signal: AbortSignal) => Promise<unknown>) => {
    try {
      return {
        ok: true,
        stale: false,
        value: await work(api, new AbortController().signal),
      };
    } catch (error) {
      return { ok: false, stale: false, error };
    }
  }) as unknown as StaffContextValue["run"];
}

function replyTextareas(container: HTMLElement): HTMLTextAreaElement[] {
  return [...container.querySelectorAll("textarea")] as HTMLTextAreaElement[];
}

function submitButton(container: HTMLElement): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")]
    .find((item) => item.textContent?.includes("提交审批"));
  if (!button) throw new Error("submit button not found");
  return button as HTMLButtonElement;
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("DraftSection", () => {
  it("keeps unsaved input after a 409 and waits for an explicit reload", async () => {
    const onReload = vi.fn();
    const context = createStaffContext({
      run: fixedRun({
        ok: false,
        stale: false,
        error: new StaffApiError(409, "revision conflict", "REVISION_CONFLICT", "trace-9"),
      }),
    });
    const { container, unmount } = await renderStaff(
      <DraftSection
        detail={detailFixture()}
        evidenceChunks={[evidenceFixture()]}

        evidenceMissingReasons={[]}
        evidenceLoading={false}
        blocking={false}
        onUpdated={() => {}}
        onReload={onReload}
      />,
      context,
    );

    await click(container, "保存为新的编辑草稿");

    const textareas = [...container.querySelectorAll("textarea")] as HTMLTextAreaElement[];
    expect(textareas[0]?.value).toBe("内部摘要-当前");
    expect(textareas[1]?.value).toBe("消费者回复-当前");
    expect(textOf(container)).toContain("版本冲突，未提交的输入已保留");
    expect(textOf(container)).toContain("读取最新版本（保留输入）");
    expect(onReload).not.toHaveBeenCalled();
    await unmount();
  });

  it("treats a 503 AI failure as manual-continue, never as a fake suggestion", async () => {
    const onUpdated = vi.fn();
    const context = createStaffContext({
      run: fixedRun({
        ok: false,
        stale: false,
        error: new StaffApiError(503, "not configured", "LLM_PROVIDER_UNAVAILABLE", null),
      }),
    });
    const { container, unmount } = await renderStaff(
      <DraftSection
        detail={detailFixture()}
        evidenceChunks={[evidenceFixture()]}

        evidenceMissingReasons={[]}
        evidenceLoading={false}
        blocking={false}
        onUpdated={onUpdated}
        onReload={() => {}}
      />,
      context,
    );

    await click(container, "AI 生成建议草稿");

    expect(textOf(container)).toContain("AI 建议不可用，可继续人工撰写草稿");
    expect(textOf(container)).toContain("不会把空结果或 Fake 输出当作 AI 成功");
    expect(onUpdated).not.toHaveBeenCalled();
    const textareas = [...container.querySelectorAll("textarea")] as HTMLTextAreaElement[];
    expect(textareas.every((item) => !item.disabled)).toBe(true);
    await unmount();
  });

  it("saves against the revision the edit started from until the latest revision is explicitly adopted", async () => {
    const createDraft = vi.fn(async (_market: string, _caseId: number, body: DraftCreateInput) => {
      if (createDraft.mock.calls.length === 1) {
        throw new StaffApiError(409, "revision conflict", "REVISION_CONFLICT", "trace-7");
      }
      expect(body.expected_revision).toBe(9);
      return detailFixture({ revision: 10 });
    });
    const api = { createDraft } as unknown as StaffApi;
    const onReload = vi.fn();
    const context = createStaffContext({ api, run: apiRun(api) });
    const element = (detail: CaseDetail) => (
      <DraftSection
        detail={detail}
        evidenceChunks={[evidenceFixture()]}
        evidenceMissingReasons={[]}
        evidenceLoading={false}
        blocking={false}
        onUpdated={() => {}}
        onReload={onReload}
      />
    );

    const { container, rerender, unmount } = await renderStaff(element(detailFixture()), context);
    await setControlValue(replyTextareas(container)[1]!, "我的修改");

    // A background refresh moves the server revision from 7 to 8.
    await rerender(element(detailFixture({ revision: 8 })));
    await pump(2);
    expect(textOf(container)).toContain("服务端数据已变化");
    expect(textOf(container)).toContain("编辑区基于数据版本 r7");

    await click(container, "保存为新的编辑草稿");
    expect(createDraft).toHaveBeenCalledTimes(1);
    expect(createDraft.mock.calls[0]![2].consumer_reply).toBe("我的修改");
    // The save must not silently rebase the old input onto revision 8.
    expect(createDraft.mock.calls[0]![2].expected_revision).toBe(7);
    expect(replyTextareas(container)[1]!.value).toBe("我的修改");
    expect(textOf(container)).toContain("版本冲突，未提交的输入已保留");

    // The user explicitly asks for the latest revision; only then may the
    // edit base move forward.
    await click(container, "读取最新版本（保留输入）");
    expect(onReload).toHaveBeenCalled();
    await rerender(element(detailFixture({ revision: 9 })));
    await pump(2);
    expect(replyTextareas(container)[1]!.value).toBe("我的修改");
    // Reading the latest version is explicit; the edit base moves only after
    // the user confirms it, never from a background refresh.
    await click(container, "确认以 r9 为保存基准");

    await click(container, "保存为新的编辑草稿");
    expect(createDraft).toHaveBeenCalledTimes(2);
    expect(createDraft.mock.calls[1]![2].expected_revision).toBe(9);

    await unmount();
  });

  it("keeps edits made while a save is pending and blocks submit until they are saved", async () => {
    const first = deferred<CaseDetail>();
    const second = deferred<CaseDetail>();
    const createDraft = vi.fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const api = { createDraft } as unknown as StaffApi;
    const context = createStaffContext({ api, run: apiRun(api) });
    const v4 = draftFixture({
      draft_id: "draft-4",
      version: 4,
      internal_summary: "内部摘要-当前",
      consumer_reply: "A",
    });
    const detail8 = detailFixture({ revision: 8, current_draft: v4, drafts: [v4] });
    const v5 = draftFixture({
      draft_id: "draft-5",
      version: 5,
      internal_summary: "内部摘要-当前",
      consumer_reply: "B",
    });
    const detail9 = detailFixture({ revision: 9, current_draft: v5, drafts: [v4, v5] });
    const element = (detail: CaseDetail) => (
      <DraftSection
        detail={detail}
        evidenceChunks={[evidenceFixture()]}
        evidenceMissingReasons={[]}
        evidenceLoading={false}
        blocking={false}
        onUpdated={() => {}}
        onReload={() => {}}
      />
    );

    const { container, rerender, unmount } = await renderStaff(element(detailFixture()), context);

    // Save A, then keep typing B while the request is still pending.
    await setControlValue(replyTextareas(container)[1]!, "A");
    await click(container, "保存为新的编辑草稿");
    expect(createDraft).toHaveBeenCalledTimes(1);
    expect(createDraft.mock.calls[0]![2].consumer_reply).toBe("A");
    await setControlValue(replyTextareas(container)[1]!, "B");
    expect(replyTextareas(container)[1]!.disabled).toBe(false);

    await act(async () => {
      first.resolve(detail8);
      await Promise.resolve();
    });
    await rerender(element(detail8));
    await pump(2);

    // Server now holds A, the UI still holds B. Submit must stay locked.
    expect(replyTextareas(container)[1]!.value).toBe("B");
    expect(textOf(container)).toContain("当前修改尚未保存");
    expect(submitButton(container).disabled).toBe(true);

    await click(container, "保存为新的编辑草稿");
    expect(createDraft).toHaveBeenCalledTimes(2);
    expect(createDraft.mock.calls[1]![2].consumer_reply).toBe("B");
    expect(createDraft.mock.calls[1]![2].expected_revision).toBe(8);

    await act(async () => {
      second.resolve(detail9);
      await Promise.resolve();
    });
    await rerender(element(detail9));
    await pump(2);

    expect(replyTextareas(container)[1]!.value).toBe("B");
    expect(textOf(container)).not.toContain("当前修改尚未保存");
    expect(submitButton(container).disabled).toBe(false);

    await unmount();
  });

  it("is read-only when the authenticated role cannot write drafts", async () => {
    const context = createStaffContext({ canSupport: false, canApprove: true });
    const { container, unmount } = await renderStaff(
      <DraftSection
        detail={detailFixture()}
        evidenceChunks={[evidenceFixture()]}

        evidenceMissingReasons={[]}
        evidenceLoading={false}
        blocking={false}
        onUpdated={() => {}}
        onReload={() => {}}
      />,
      context,
    );
    await settle();
    expect(textOf(container)).toContain("只读视图");
    expect(textOf(container)).not.toContain("保存为新的编辑草稿");
    expect(textOf(container)).not.toContain("提交审批");
    await unmount();
  });
});
