// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { StaffApiError } from "./api";
import type { StaffContextValue } from "./context";
import type { RunResult } from "./guard";
import {
  click,
  createStaffContext,
  renderStaff,
  settle,
  textOf,
  ticketFixture,
} from "./testSupport";
import { TicketSection } from "./TicketSection";
import type { CaseTicketResponse } from "./types";

describe("TicketSection", () => {
  it("keeps the last verified ticket and marks it stale when a newer read fails", async () => {
    const results: Array<RunResult<CaseTicketResponse>> = [
      { ok: true, stale: false, value: ticketFixture() },
      {
        ok: false,
        stale: false,
        error: new StaffApiError(503, "connector down", "CONNECTOR_UNAVAILABLE", "trace-3"),
      },
    ];
    const run = (async () => results.shift()) as unknown as StaffContextValue["run"];
    const context = createStaffContext({ run, canSupport: true });
    const { container, unmount } = await renderStaff(
      <TicketSection caseId={1} remoteTicketId="ticket-1" />,
      context,
    );
    await settle();

    expect(textOf(container)).toContain("已经为您处理退款");
    expect(textOf(container)).toContain("本次已核验");
    expect(textOf(container)).toContain("真实回执：receipt-1");

    await click(container, "查询真实工单");

    expect(textOf(container)).toContain("本次查询失败，已保留上次核验内容");
    expect(textOf(container)).toContain("陈旧结果");
    expect(textOf(container)).toContain("不能当作最新履行状态");
    expect(textOf(container)).toContain("已经为您处理退款");
    await unmount();
  });
});
