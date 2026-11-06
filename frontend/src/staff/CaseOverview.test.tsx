// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { CaseOverview } from "./CaseOverview";
import type { CaseOverviewProps } from "./CaseOverview";
import {
  approvalFixture,
  attachmentFixture,
  caseEvidenceFixture,
  detailFixture,
  draftFixture,
  operationFixture,
  orderFixture,
  renderUi,
  textOf,
} from "./testSupport";
import type { CaseDetail, StaffIdentity } from "./types";

const supportIdentity: StaffIdentity = {
  staff_id: "me",
  display_name: "客服",
  roles: ["support"],
  allowed_markets: ["au"],
};

const approverIdentity: StaffIdentity = {
  staff_id: "me",
  display_name: "审批人",
  roles: ["approver"],
  allowed_markets: ["au"],
};

function overview(
  props: Partial<CaseOverviewProps> & { detail: CaseDetail },
) {
  return (
    <CaseOverview
      evidence={caseEvidenceFixture()}
      evidenceLoading={false}
      evidenceError={null}
      identity={supportIdentity}
      canSupport
      canApprove={false}
      canAudit={false}
      {...props}
    />
  );
}

function nextStepTitle(container: HTMLElement): string {
  return container.querySelector(".staff-overview-next h3")?.textContent ?? "";
}

function factTexts(container: HTMLElement): string[] {
  return [...container.querySelectorAll(".staff-fact")].map(
    (fact) => fact.textContent ?? "",
  );
}

const nextStepCases: Array<[string, CaseDetail, string]> = [
  ["no draft", detailFixture({ current_draft: null, drafts: [] }), "形成处理草稿"],
  ["editing draft", detailFixture(), "核对草稿并送审"],
  ["needs info", detailFixture({ state: "needs_info" }), "补充材料或证据"],
  ["executing", detailFixture({ state: "executing" }), "执行中，等待真实回执"],
  [
    "reconciliation required",
    detailFixture({ state: "reconciliation_required" }),
    "结果待核对",
  ],
  ["completed", detailFixture({ state: "completed" }), "已完成"],
  ["failed", detailFixture({ state: "failed" }), "执行失败"],
];

describe("CaseOverview", () => {
  it("tells support that a submitted case is waiting for an approver, not completed", async () => {
    const detail = detailFixture({
      state: "waiting_approval",
      current_draft: draftFixture({ status: "pending_approval" }),
      drafts: [draftFixture({ status: "pending_approval" })],
      approvals: [approvalFixture({ requester_staff_id: "requester" })],
    });
    const { container, unmount } = await renderUi(overview({ detail }));

    expect(nextStepTitle(container)).toContain("等待审批人");
    expect(textOf(container)).toContain("尚未创建工单");
    expect(textOf(container)).not.toContain("已完成");
    await unmount();
  });

  it("tells a non-requester approver that their decision is required", async () => {
    const detail = detailFixture({
      state: "waiting_approval",
      current_draft: draftFixture({ status: "pending_approval" }),
      drafts: [draftFixture({ status: "pending_approval" })],
      approvals: [approvalFixture({ requester_staff_id: "requester" })],
    });
    const { container, unmount } = await renderUi(overview({
      detail,
      identity: approverIdentity,
      canSupport: false,
      canApprove: true,
    }));

    expect(nextStepTitle(container)).toContain("需要你审批");
    expect(textOf(container)).toContain("尚未创建工单");
    await unmount();
  });

  it("blocks approval and warns when a pending approval lacks a frozen snapshot", async () => {
    const detail = detailFixture({
      state: "waiting_approval",
      current_draft: draftFixture({ status: "pending_approval" }),
      drafts: [draftFixture({ status: "pending_approval" })],
      approvals: [approvalFixture({
        requester_staff_id: "requester",
        snapshot: null,
      })],
    });
    const { container, unmount } = await renderUi(overview({
      detail,
      identity: approverIdentity,
      canSupport: false,
      canApprove: true,
    }));

    expect(nextStepTitle(container)).toContain("审批冻结快照缺失");
    expect(nextStepTitle(container)).not.toContain("等待审批人");
    const text = textOf(container);
    expect(text).toContain("当前诉求不能批准");
    expect(text).toContain("管理员检查数据完整性");
    expect(text).toContain("待审批项的冻结快照缺失");
    await unmount();
  });

  it("tells the requester who is also an approver that another approver is needed", async () => {
    const detail = detailFixture({
      state: "waiting_approval",
      current_draft: draftFixture({ status: "pending_approval" }),
      drafts: [draftFixture({ status: "pending_approval" })],
      approvals: [approvalFixture({ requester_staff_id: "me" })],
    });
    const { container, unmount } = await renderUi(overview({
      detail,
      identity: approverIdentity,
      canSupport: false,
      canApprove: true,
    }));

    expect(nextStepTitle(container)).toContain("需另一位审批人");
    expect(textOf(container)).toContain("不能审批自己的草稿");
    await unmount();
  });

  it.each(nextStepCases)("renders an accurate next step for %s", async (_label, detail, expected) => {
    const { container, unmount } = await renderUi(overview({ detail }));

    expect(nextStepTitle(container)).toContain(expected);
    await unmount();
  });

  it("shows the four key facts and keeps evidence gaps in the summary", async () => {
    const detail = detailFixture({
      attachments: [
        attachmentFixture(),
        attachmentFixture({ attachment_id: "att-2", file_name: "box.png" }),
      ],
    });
    const evidence = caseEvidenceFixture({
      missing_reasons: ["missing_policy_evidence"],
    });
    const { container, unmount } = await renderUi(overview({ detail, evidence }));

    const facts = factTexts(container);
    expect(facts).toHaveLength(4);
    expect(facts.join("\n")).toContain("订单及商品");
    expect(facts.join("\n")).toContain("客户附件数");
    expect(facts.join("\n")).toContain("2 个");
    expect(facts.join("\n")).toContain("政策证据数");
    expect(facts.join("\n")).toContain("1 条");
    expect(textOf(container)).toContain("政策证据存在缺资料或限制");
    expect(textOf(container)).toContain("没有匹配的政策证据");
    await unmount();
  });

  it("counts product quantity rather than order line items", async () => {
    const detail = detailFixture({
      order: orderFixture({
        items: [
          {
            order_item_id: "item-1",
            sku: "SKU-1",
            name: "测试商品",
            quantity: 2,
            unit_price_minor: 1299,
            line_total_minor: 2598,
          },
          {
            order_item_id: "item-2",
            sku: "SKU-2",
            name: "赠品",
            quantity: 1,
            unit_price_minor: 0,
            line_total_minor: 0,
          },
        ],
        total_minor: 2598,
      }),
    });
    const { container, unmount } = await renderUi(overview({ detail }));

    const facts = factTexts(container).join("\n");
    expect(facts).toContain("3 件商品");
    expect(facts).not.toContain("2 件商品");
    await unmount();
  });

  it("marks evidence as loading or failed in the key facts", async () => {
    const loading = await renderUi(overview({
      detail: detailFixture(),
      evidence: null,
      evidenceLoading: true,
    }));
    expect(factTexts(loading.container).join("\n")).toContain("读取中");
    expect(textOf(loading.container)).toContain("正在读取政策证据");
    await loading.unmount();

    const failed = await renderUi(overview({
      detail: detailFixture(),
      evidence: null,
      evidenceError: "政策服务不可用",
    }));
    expect(factTexts(failed.container).join("\n")).toContain("读取失败");
    expect(textOf(failed.container)).toContain("政策证据读取失败");
    expect(textOf(failed.container)).toContain("政策服务不可用");
    await failed.unmount();
  });

  it("keeps approval expiry and manual verification warnings in the summary", async () => {
    const now = Date.parse("2026-11-06T00:00:00Z");
    const detail = detailFixture({
      state: "waiting_approval",
      current_draft: draftFixture({ status: "pending_approval" }),
      drafts: [draftFixture({ status: "pending_approval" })],
      approvals: [approvalFixture({
        requester_staff_id: "requester",
        expires_at: "2026-11-06T12:00:00Z",
      })],
      operations: [operationFixture({
        status: "reconciliation_required",
        reconciliation_state: "manual_review",
      })],
    });
    const { container, unmount } = await renderUi(overview({ detail, now }));

    expect(textOf(container)).toContain("审批临近过期或已过期");
    expect(textOf(container)).toContain("24 小时内过期");
    expect(textOf(container)).toContain("存在待人工核对的操作");
    await unmount();
  });

  it("keeps technical identifiers in a closed disclosure", async () => {
    const detail = detailFixture();
    const { container, unmount } = await renderUi(overview({ detail }));

    const details = container.querySelector("details.staff-id-details") as HTMLDetailsElement | null;
    expect(details).not.toBeNull();
    expect(details!.open).toBe(false);
    expect(details!.querySelector("summary")?.textContent).toContain("案件标识与版本");
    expect(textOf(details!)).toContain(detail.request_id);
    expect(textOf(details!)).toContain(detail.order_id);
    await unmount();
  });
});
