// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { Panel } from "./StaffBits";
import { click, renderUi, textOf } from "./testSupport";

describe("Panel disclosure", () => {
  it("is collapsed by default, exposes aria state, and keeps content mounted", async () => {
    const { container, unmount } = await renderUi(
      <Panel title="折叠测试" subtitle="分区摘要" collapsible defaultOpen={false}>
        <p>分区内容</p>
      </Panel>,
    );

    const toggle = container.querySelector(".staff-disclosure-toggle") as HTMLButtonElement;
    const body = container.querySelector(".staff-panel-body") as HTMLElement;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.getAttribute("aria-controls")).toBe(body.id);
    expect(body.hidden).toBe(true);
    expect(textOf(container)).toContain("分区内容");

    await click(container, "折叠测试");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(body.hidden).toBe(false);

    await click(container, "折叠测试");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(body.hidden).toBe(true);
    expect(textOf(container)).toContain("分区内容");

    await unmount();
  });

  it("shows header actions only while the panel is open or non-collapsible", async () => {
    const { container, unmount } = await renderUi(
      <Panel
        title="折叠操作"
        collapsible
        defaultOpen={false}
        actions={<button type="button">重新检索</button>}
      >
        <p>内容</p>
      </Panel>,
    );

    expect(container.textContent).not.toContain("重新检索");

    await click(container, "折叠操作");
    expect(container.textContent).toContain("重新检索");

    await click(container, "折叠操作");
    expect(container.textContent).not.toContain("重新检索");
    await unmount();

    const alwaysOpen = await renderUi(
      <Panel title="固定操作" actions={<button type="button">刷新</button>}>
        <p>内容</p>
      </Panel>,
    );
    expect(alwaysOpen.container.textContent).toContain("刷新");
    await alwaysOpen.unmount();
  });

  it("supports an explicitly open default", async () => {
    const { container, unmount } = await renderUi(
      <Panel title="默认展开" collapsible defaultOpen>
        <p>默认可见内容</p>
      </Panel>,
    );

    const toggle = container.querySelector(".staff-disclosure-toggle") as HTMLButtonElement;
    const body = container.querySelector(".staff-panel-body") as HTMLElement;
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(body.hidden).toBe(false);

    await unmount();
  });
});
