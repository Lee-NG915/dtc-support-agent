// @vitest-environment jsdom
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import StaffApp from "./StaffApp";
import type { CaseListResponse, StaffIdentity } from "./types";
import {
  flushMicrotasks,
  pump,
  renderUi,
  setControlValue,
  textOf,
  waitFor,
} from "./testSupport";

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function identity(
  staffId: string,
  displayName: string,
  roles: StaffIdentity["roles"],
  markets: StaffIdentity["allowed_markets"],
): StaffIdentity {
  return {
    staff_id: staffId,
    display_name: displayName,
    roles,
    allowed_markets: markets,
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

function headersOf(init?: RequestInit): Record<string, string> {
  return (init?.headers as Record<string, string> | undefined) ?? {};
}

async function submitDemoPassword(container: HTMLElement, password: string): Promise<void> {
  const input = container.querySelector('input[name="password"]') as HTMLInputElement | null;
  const form = container.querySelector("form") as HTMLFormElement | null;
  if (!input || !form) throw new Error("demo login form not found");
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  await act(async () => {
    setter?.call(input, password);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await flushMicrotasks();
  });
  await pump(6);
}

function demoAccounts(): StaffIdentity[] {
  return [
    identity("staff_approver_au", "Synthetic Approver AU", ["approver"], ["au", "sg"]),
    identity("staff_support_au", "Synthetic Support AU", ["support"], ["au"]),
    identity("staff_support_us", "Synthetic Support US", ["support"], ["us"]),
  ];
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("StaffApp demo password login", () => {
  it("defaults to support, keeps the token in memory, and allows identity switching", async () => {
    const demoBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/login-options")) {
        return jsonResponse({ mode: "demo_password", accounts: demoAccounts() });
      }
      if (url.endsWith("/demo-login")) {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        demoBodies.push(body);
        const accounts = demoAccounts();
        const account = accounts.find((item) => item.staff_id === body.staff_id)!;
        const token =
          body.staff_id === "staff_approver_au"
            ? "dtc_demo.approver-session"
            : "dtc_demo.support-session";
        return jsonResponse({
          token,
          token_type: "Bearer",
          expires_in: 28800,
          identity: account,
        });
      }
      if (url.includes("/au/cases")) return jsonResponse(emptyList("au"));
      if (url.includes("/sg/cases")) return jsonResponse(emptyList("sg"));
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container, unmount } = await renderUi(<StaffApp />);
    await waitFor(() => {
      const select = container.querySelector('select[name="staff_id"]');
      return select !== null && (select as HTMLSelectElement).value === "staff_support_au";
    }, "default support identity");

    expect(textOf(container)).toContain("选择一个合成员工身份并使用演示密码登录");
    expect(container.querySelector('input[name="token"]')).toBeNull();
    expect((container.querySelector('input[name="password"]') as HTMLInputElement).type).toBe(
      "password",
    );

    await submitDemoPassword(container, "demo-password-123");
    await waitFor(() => textOf(container).includes("诉求队列"), "support workbench");
    expect(textOf(container)).toContain("Synthetic Support AU");
    expect(demoBodies[0]).toEqual({
      staff_id: "staff_support_au",
      password: "demo-password-123",
    });
    const supportCaseCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/au/cases"),
    ) as [string, RequestInit] | undefined;
    expect(headersOf(supportCaseCall?.[1]).Authorization).toBe(
      "Bearer dtc_demo.support-session",
    );
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(window.location.href).not.toContain("demo-password-123");

    await act(async () => {
      [...container.querySelectorAll("button")]
        .find((button) => button.textContent?.includes("退出"))!
        .click();
      await flushMicrotasks();
    });
    await waitFor(
      () => container.querySelector('select[name="staff_id"]') !== null,
      "demo form after logout",
    );
    expect(textOf(container)).toContain("已退出");
    expect(textOf(container)).not.toContain("诉求队列");

    const select = container.querySelector('select[name="staff_id"]') as HTMLSelectElement;
    await setControlValue(select, "staff_approver_au");
    await submitDemoPassword(container, "demo-password-123");
    await waitFor(() => textOf(container).includes("Synthetic Approver AU"), "approver workbench");
    expect(demoBodies[1]).toEqual({
      staff_id: "staff_approver_au",
      password: "demo-password-123",
    });
    expect(window.localStorage.length).toBe(0);

    await unmount();
  });

  it("shows one generic Chinese error for a rejected identity/password pair", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("/login-options")) {
        return jsonResponse({ mode: "demo_password", accounts: demoAccounts() });
      }
      if (url.endsWith("/demo-login")) {
        return jsonResponse(
          { error_code: "UNAUTHORIZED", message: "invalid demo login" },
          401,
        );
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container, unmount } = await renderUi(<StaffApp />);
    await waitFor(
      () => container.querySelector('input[name="password"]') !== null,
      "demo form",
    );
    await submitDemoPassword(container, "wrong-password");

    await waitFor(
      () => textOf(container).includes("登录失败，请确认身份和密码后重试。"),
      "generic login error",
    );
    const text = textOf(container);
    expect(text).not.toContain("invalid demo login");
    expect(text).not.toContain("不存在");
    expect(text).not.toContain("禁用");
    expect(text).not.toContain("过期");
    expect(container.querySelector('input[name="password"]')).not.toBeNull();

    await unmount();
  });

  it("falls back to the existing bearer form when login options fail", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("/login-options")) {
        return jsonResponse({ error_code: "NOT_FOUND", message: "not found" }, 404);
      }
      if (url.endsWith("/me")) {
        return jsonResponse(identity("staff_support_au", "Bearer Staff", ["support"], ["au"]));
      }
      if (url.includes("/au/cases")) return jsonResponse(emptyList("au"));
      if (url.includes("/sg/cases")) return jsonResponse(emptyList("sg"));
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container, unmount } = await renderUi(<StaffApp />);
    await waitFor(
      () => textOf(container).includes("无法读取演示登录配置，仍可使用员工凭证登录。"),
      "token fallback notice",
    );
    const input = container.querySelector('input[name="token"]') as HTMLInputElement;
    const form = container.querySelector("form") as HTMLFormElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    await act(async () => {
      setter?.call(input, "dtc_staff_bearer_token");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await flushMicrotasks();
    });
    await pump(6);

    await waitFor(() => textOf(container).includes("Bearer Staff"), "bearer workbench");
    const meCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/me")) as
      | [string, RequestInit]
      | undefined;
    expect(headersOf(meCall?.[1]).Authorization).toBe("Bearer dtc_staff_bearer_token");

    await unmount();
  });
});
