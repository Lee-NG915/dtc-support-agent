// @vitest-environment jsdom
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import StaffApp from "./StaffApp";
import type { CaseListResponse } from "./types";
import {
  chooseOption,
  comboboxByLabel,
  findCombobox,
  flushMicrotasks,
  pump,
  renderUi,
  submitLogin,
  textOf,
  waitFor,
} from "./testSupport";

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function identity(displayName: string) {
  return {
    staff_id: displayName,
    display_name: displayName,
    roles: ["support"],
    allowed_markets: ["au", "sg"],
  };
}

function emptyList(market: "au" | "sg"): CaseListResponse {
  return {
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
    has_more: false,
    last_success_at: "2026-11-06T00:00:00Z",
    market,
  } as CaseListResponse;
}

function authorizationOf(init?: RequestInit): string {
  return String((init?.headers as Record<string, string> | undefined)?.Authorization ?? "");
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("StaffApp session lifecycle", () => {
  it("survives login, workbench, logout and a second login with the same hook order", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/login-options")) {
        return jsonResponse({ mode: "staff_token", accounts: [] });
      }
      if (url.endsWith("/me")) {
        return jsonResponse(identity(
          authorizationOf(init) === "Bearer token-two" ? "员工二" : "员工一",
        ));
      }
      if (url.includes("/au/cases")) return jsonResponse(emptyList("au"));
      if (url.includes("/sg/cases")) return jsonResponse(emptyList("sg"));
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container, unmount } = await renderUi(<StaffApp />);

    await submitLogin(container, "token-one");
    await waitFor(() => textOf(container).includes("诉求队列"), "first workbench");
    expect(textOf(container)).toContain("员工一");
    expect(container.querySelector('input[name="token"]')).toBeNull();

    await act(async () => {
      [...container.querySelectorAll("button")]
        .find((button) => button.textContent?.includes("退出"))!
        .click();
      await flushMicrotasks();
    });
    await waitFor(() => textOf(container).includes("登录工作台"), "login page after logout");
    expect(textOf(container)).toContain("已退出");
    expect(textOf(container)).not.toContain("诉求队列");
    expect(container.querySelector('input[name="token"]')).not.toBeNull();

    await submitLogin(container, "token-two");
    await waitFor(() => textOf(container).includes("员工二"), "second identity");
    expect(textOf(container)).toContain("诉求队列");
    expect(textOf(container)).not.toContain("登录工作台");

    await unmount();
  });
});

describe("StaffApp generation-scoped retries", () => {
  it("never lets a delayed old-market retry borrow the new identity or sign it out", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    let auCalls = 0;
    let sgCalls = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("/login-options")) {
        return jsonResponse({ mode: "staff_token", accounts: [] });
      }
      if (url.endsWith("/me")) return jsonResponse(identity("员工一"));
      if (url.includes("/au/cases")) {
        auCalls += 1;
        if (auCalls === 1) {
          return jsonResponse({ error_code: "INTERNAL_ERROR", message: "boom" }, 500);
        }
        // If the old retry wrongly borrows the new generation, this late 403
        // would sign the freshly selected identity out.
        return jsonResponse({ error_code: "FORBIDDEN_ROLE", message: "old scope" }, 403);
      }
      if (url.includes("/sg/cases")) {
        sgCalls += 1;
        return jsonResponse(emptyList("sg"));
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container, unmount } = await renderUi(<StaffApp />);
    await submitLogin(container, "token-one");
    await waitFor(
      () => textOf(container).includes("正在进行第 1 次有限重试"),
      "first queued retry",
    );
    expect(auCalls).toBe(1);

    await chooseOption(container, "选择授权市场", "新加坡 (SG)");
    await waitFor(() => sgCalls > 0, "sg queue request");
    await pump(4);

    // Let the old 700ms retry delay elapse. The old closure must not send an
    // au request with the new token/generation.
    await act(async () => {
      vi.advanceTimersByTime(2_000);
      await flushMicrotasks(60);
    });
    await pump(4);

    expect(auCalls).toBe(1);
    expect(sgCalls).toBeGreaterThanOrEqual(1);
    expect(textOf(container)).toContain("员工一");
    expect(textOf(container)).not.toContain("请重新登录");
    expect(container.querySelector('input[name="token"]')).toBeNull();
    expect(comboboxByLabel(container, "选择授权市场").textContent).toContain("新加坡 (SG)");

    await unmount();
  });

  it("ignores a late 403 from an in-flight old generation", async () => {
    let resolveAu!: (response: Response) => void;
    const pendingAu = new Promise<Response>((resolve) => { resolveAu = resolve; });
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("/login-options")) {
        return jsonResponse({ mode: "staff_token", accounts: [] });
      }
      if (url.endsWith("/me")) return jsonResponse(identity("员工一"));
      if (url.includes("/au/cases")) return pendingAu;
      if (url.includes("/sg/cases")) return jsonResponse(emptyList("sg"));
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container, unmount } = await renderUi(<StaffApp />);
    await submitLogin(container, "token-one");
    await waitFor(
      () => findCombobox(container, "选择授权市场") !== null,
      "market combobox",
    );

    await chooseOption(container, "选择授权市场", "新加坡 (SG)");
    await pump(4);

    await act(async () => {
      resolveAu(jsonResponse({ error_code: "FORBIDDEN_ROLE", message: "old identity revoked" }, 403));
      await flushMicrotasks(60);
    });
    await pump(4);

    expect(textOf(container)).toContain("员工一");
    expect(textOf(container)).not.toContain("请重新登录");
    expect(container.querySelector('input[name="token"]')).toBeNull();

    await unmount();
  });
});
