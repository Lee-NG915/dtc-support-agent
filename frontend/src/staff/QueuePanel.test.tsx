// @vitest-environment jsdom
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";
import type { StaffApi } from "./api";
import type { StaffContextValue } from "./context";
import { QueuePanel } from "./QueuePanel";
import {
  createStaffContext,
  flushMicrotasks,
  renderStaff,
  textOf,
  waitFor,
} from "./testSupport";

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

function localDateKey(date: Date): string {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("QueuePanel date filters", () => {
  it("applies a custom DatePicker date and keeps the calendar closed", async () => {
    const calls: Array<Record<string, unknown>> = [];
    const api = {
      listCases: async (_market: string, params: Record<string, unknown>) => {
        calls.push(params);
        return {
          items: [],
          total: 0,
          limit: 20,
          offset: 0,
          has_more: false,
          last_success_at: "2026-11-06T00:00:00Z",
          market: "au",
        };
      },
    } as unknown as StaffApi;
    const run = (async (
      work: (client: StaffApi, signal: AbortSignal) => Promise<unknown>,
    ) => {
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
    const context = createStaffContext({ api, run });
    const { container, unmount } = await renderStaff(
      <QueuePanel selectedCaseId={null} onSelect={() => {}} />,
      context,
    );

    await waitFor(
      () => calls.length > 0 && textOf(container).includes("当前市场暂无诉求"),
      "queue loaded",
    );
    const callsBefore = calls.length;

    const trigger = container.querySelector(
      'button[aria-label="更新起始日期"]',
    ) as HTMLButtonElement;
    expect(trigger).not.toBeNull();
    await act(async () => {
      trigger.click();
      await flushMicrotasks();
    });
    expect(container.querySelector('[role="dialog"]')).not.toBeNull();

    const today = new Date();
    const todayCell = container.querySelector(
      '.fc-day[aria-current="date"]',
    ) as HTMLButtonElement;
    expect(todayCell).not.toBeNull();
    await act(async () => {
      todayCell.click();
      await flushMicrotasks();
    });
    await waitFor(() => calls.length > callsBefore, "date-filtered queue request");

    // A wrapping label would re-click the trigger and reopen the calendar here.
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(trigger.textContent).toContain(new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "short",
      day: "numeric",
    }).format(today));
    expect(calls.at(-1)?.updated_from).toBe(
      new Date(`${localDateKey(today)}T00:00:00`).toISOString(),
    );

    await unmount();
  });
});
