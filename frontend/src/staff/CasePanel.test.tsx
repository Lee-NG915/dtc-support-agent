// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { StaffApiError } from "./api";
import type { StaffApi } from "./api";
import { CasePanel } from "./CasePanel";
import type { StaffContextValue } from "./context";
import {
  attachmentFixture,
  caseEvidenceFixture,
  click,
  createStaffContext,
  detailFixture,
  materialFixture,
  renderStaff,
  setControlValue,
  textOf,
  waitFor,
} from "./testSupport";

function panel(container: HTMLElement, label: string): HTMLElement {
  const element = container.querySelector(`section[aria-label="${label}"]`);
  if (element === null) throw new Error(`panel not found: ${label}`);
  return element as HTMLElement;
}

function disclosure(container: HTMLElement, label: string): {
  expanded: string | null;
  hidden: boolean;
} {
  const section = panel(container, label);
  const toggle = section.querySelector(".staff-disclosure-toggle") as HTMLButtonElement | null;
  const body = section.querySelector(".staff-panel-body") as HTMLElement | null;
  if (toggle === null || body === null) throw new Error(`disclosure not found: ${label}`);
  return {
    expanded: toggle.getAttribute("aria-expanded"),
    hidden: body.hasAttribute("hidden"),
  };
}

function mockCaseApi() {
  const detail = detailFixture({
    state: "processing",
    description: "消费者描述：商品有破损。",
    attachments: [attachmentFixture()],
    materials: [materialFixture()],
  });
  const evidence = caseEvidenceFixture();
  const api = {
    getCase: async () => detail,
    getEvents: async () => ({
      items: [],
      after_seq: 0,
      limit: 50,
      has_more: false,
    }),
    getEvidence: async () => evidence,
    getShipment: async () => {
      throw new StaffApiError(404, "shipment not found", "NOT_FOUND", null);
    },
    getOrder: async () => detail.order,
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
  const context = createStaffContext({
    api,
    run,
    canSupport: true,
    canApprove: false,
    canAudit: false,
    identity: {
      staff_id: "me",
      display_name: "客服",
      roles: ["support"],
      allowed_markets: ["au"],
    },
  });
  return { detail, context };
}

describe("CasePanel summary-first hierarchy", () => {
  it("shows the next step first and collapses secondary sections by default", async () => {
    const { context } = mockCaseApi();
    const { container, unmount } = await renderStaff(<CasePanel caseId={1} />, context);
    await waitFor(
      () => textOf(container).includes("核对草稿并送审"),
      "case overview",
    );
    await waitFor(() => textOf(container).includes("可用 1 条"), "evidence summary");

    expect(textOf(container)).toContain("消费者描述：商品有破损。");
    expect(textOf(container)).toContain("客户原话");

    expect(disclosure(container, "附件与内部材料")).toEqual({
      expanded: "false",
      hidden: true,
    });
    expect(disclosure(container, "订单与物流")).toEqual({
      expanded: "false",
      hidden: true,
    });
    expect(disclosure(container, "政策证据")).toEqual({
      expanded: "false",
      hidden: true,
    });
    expect(disclosure(container, "草稿与送审")).toEqual({
      expanded: "true",
      hidden: false,
    });
    expect(disclosure(container, "审批与冻结依据")).toEqual({
      expanded: "false",
      hidden: true,
    });
    expect(disclosure(container, "执行与操作状态")).toEqual({
      expanded: "false",
      hidden: true,
    });
    expect(disclosure(container, "真实工单读取")).toEqual({
      expanded: "false",
      hidden: true,
    });
    expect(disclosure(container, "事件时间线")).toEqual({
      expanded: "false",
      hidden: true,
    });

    expect(panel(container, "附件与内部材料").textContent).toContain(
      "消费者附件 1 个 · 内部材料 1 条",
    );
    expect(panel(container, "订单与物流").textContent).toContain("物流读取：展开后查询");
    expect(panel(container, "政策证据").textContent).toContain("可用 1 条");
    expect(panel(container, "订单与物流").querySelector(".staff-panel-actions button")).toBeNull();
    expect(panel(container, "政策证据").querySelector(".staff-panel-actions button")).toBeNull();
    expect(panel(container, "真实工单读取").querySelector(".staff-panel-actions button")).toBeNull();
    expect(panel(container, "事件时间线").querySelector(".staff-panel-actions button")).toBeNull();
    await unmount();
  });

  it("does not lose unsaved draft input when the draft section is collapsed", async () => {
    const { context } = mockCaseApi();
    const { container, unmount } = await renderStaff(<CasePanel caseId={1} />, context);
    await waitFor(
      () => textOf(container).includes("核对草稿并送审"),
      "case overview",
    );

    const draftPanel = panel(container, "草稿与送审");
    const reply = draftPanel.querySelectorAll("textarea")[1] as HTMLTextAreaElement;
    await setControlValue(reply, "未保存的修改");

    await click(container, "草稿与送审");
    expect(disclosure(container, "草稿与送审")).toEqual({
      expanded: "false",
      hidden: true,
    });

    await click(container, "草稿与送审");
    expect(disclosure(container, "草稿与送审")).toEqual({
      expanded: "true",
      hidden: false,
    });
    expect((draftPanel.querySelectorAll("textarea")[1] as HTMLTextAreaElement).value)
      .toBe("未保存的修改");
    expect(textOf(container)).toContain("有未保存修改");
    await unmount();
  });
});
