// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import type { StaffApi } from "./api";
import type { StaffContextValue } from "./context";
import { OperatorPanel } from "./OperatorPanel";
import {
  click,
  createStaffContext,
  operationDetailFixture,
  renderStaff,
  textOf,
} from "./testSupport";
import type { WorkerOperationListResponse } from "./types";

describe("OperatorPanel", () => {
  it("uses only operator endpoints and never reads business detail, attachments, evidence or chat", async () => {
    const listOperations = vi.fn(async (): Promise<WorkerOperationListResponse> => ({
      items: [{
        operation_id: "op-1",
        case_id: 1,
        market: "au",
        status: "awaiting_receipt",
        case_state: "executing",
        attempt: 1,
        lookup_count: 0,
        reconciliation_state: "checking",
        remote_ticket_id: "ticket-1",
        last_error_code: null,
        failure_reason: null,
        dispatched_at: "2026-11-06T01:00:00Z",
        next_attempt_at: null,
        updated_at: "2026-11-06T01:00:00Z",
      }],
      total: 1,
      limit: 20,
      offset: 0,
      has_more: false,
    }));
    const getOperation = vi.fn(async () => operationDetailFixture());
    const getCase = vi.fn();
    const getEvidence = vi.fn();
    const getAttachment = vi.fn();
    const streamChat = vi.fn();
    const api = {
      listOperations,
      getOperation,
      getCase,
      getEvidence,
      getAttachment,
      streamChat,
    } as unknown as StaffApi;
    const run = (async (work: (client: StaffApi, signal: AbortSignal) => Promise<unknown>) => {
      try {
        return { ok: true, stale: false, value: await work(api, new AbortController().signal) };
      } catch (error) {
        return { ok: false, stale: false, error };
      }
    }) as unknown as StaffContextValue["run"];
    const context = createStaffContext({
      api,
      run,
      canSupport: false,
      canApprove: false,
      canAudit: false,
      canOperate: true,
      identity: {
        staff_id: "operator-1",
        display_name: "运营",
        roles: ["operator"],
        allowed_markets: ["au"],
      },
    });
    const { container, unmount } = await renderStaff(<OperatorPanel />, context);

    expect(textOf(container)).toContain("不读取消费者业务详情、附件、证据或聊天内容");
    await click(container, "等待真实回执");

    expect(listOperations).toHaveBeenCalled();
    expect(getOperation).toHaveBeenCalledWith("au", "op-1", expect.any(AbortSignal));
    expect(getCase).not.toHaveBeenCalled();
    expect(getEvidence).not.toHaveBeenCalled();
    expect(getAttachment).not.toHaveBeenCalled();
    expect(streamChat).not.toHaveBeenCalled();
    const buttonLabels = [...container.querySelectorAll("button")]
      .map((button) => button.textContent ?? "");
    expect(buttonLabels.some((label) => label.includes("标记成功") || label.includes("标记完成"))).toBe(false);
    await unmount();
  });
});
