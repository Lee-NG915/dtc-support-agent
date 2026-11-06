import { afterEach, describe, expect, it, vi } from "vitest";
import { StaffApi, demoLogin, getLoginOptions } from "./api";
import { detailFixture } from "./testSupport";

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function sseResponse(frames: string): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(frames));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("staff demo login requests", () => {
  it("discovers the login mode and exchanges a password only in the demo body", async () => {
    const identity = {
      staff_id: "staff_support_au",
      display_name: "Synthetic Support AU",
      roles: ["support"],
      allowed_markets: ["au"],
    };
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("/login-options")) {
        return jsonResponse({ mode: "demo_password", accounts: [identity] });
      }
      if (url.endsWith("/demo-login")) {
        return jsonResponse({
          token: "dtc_demo.signed-token",
          token_type: "Bearer",
          expires_in: 28800,
          identity,
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;

    const options = await getLoginOptions(signal);
    expect(options).toEqual({ mode: "demo_password", accounts: [identity] });

    const session = await demoLogin("staff_support_au", "demo-password-123", signal);
    expect(session).toEqual({
      token: "dtc_demo.signed-token",
      token_type: "Bearer",
      expires_in: 28800,
      identity,
    });

    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>;
    const [optionsUrl, optionsInit] = calls[0]!;
    expect(optionsUrl).toBe("/api/v1/after-sales/login-options");
    expect(optionsInit.method).toBeUndefined();
    expect(
      (optionsInit.headers as Record<string, string>).Authorization,
    ).toBeUndefined();

    const [demoUrl, demoInit] = calls[1]!;
    expect(demoUrl).toBe("/api/v1/after-sales/demo-login");
    expect(demoInit.method).toBe("POST");
    const demoHeaders = demoInit.headers as Record<string, string>;
    expect(demoHeaders["Content-Type"]).toBe("application/json");
    expect(demoHeaders.Authorization).toBeUndefined();
    expect(JSON.parse(String(demoInit.body))).toEqual({
      staff_id: "staff_support_au",
      password: "demo-password-123",
    });
    expect(String(demoInit.body)).not.toContain("Bearer");
  });
});

describe("staff API requests", () => {
  it("keeps the bearer in memory, scopes chat to epig + market, and never sends confirm_action_id", async () => {
    const fetchMock = vi.fn(async () => sseResponse(
      'data: {"event":"token","text":"根据政策"}\n\n' +
      'data: {"event":"answer","data":{"answer":"根据政策，需要材料。","session_id":"s-1","trace_id":"t-1","citations":[],"refused":false,"error_code":null}}\n\n',
    ));
    vi.stubGlobal("fetch", fetchMock);
    const api = new StaffApi("memory-only-token");
    const events: unknown[] = [];

    await api.streamChat(
      { message: "退货政策", user_id: "staff-1", brand: "epig", market: "sg" },
      (event) => events.push(event),
      new AbortController().signal,
    );

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/chat/stream");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer memory-only-token");
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(body).toMatchObject({ brand: "epig", market: "sg", user_id: "staff-1" });
    expect(body).not.toHaveProperty("confirm_action_id");
    expect(events).toEqual([
      { event: "token", text: "根据政策" },
      {
        event: "answer",
        data: {
          answer: "根据政策，需要材料。",
          session_id: "s-1",
          trace_id: "t-1",
          citations: [],
          refused: false,
          error_code: null,
        },
      },
    ]);
  });

  it("propagates abort to the bearer-authenticated stream", async () => {
    const fetchMock = vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => {
        reject(new DOMException("aborted", "AbortError"));
      });
    }));
    vi.stubGlobal("fetch", fetchMock);
    const api = new StaffApi("memory-only-token");
    const controller = new AbortController();

    const pending = api.streamChat(
      { message: "问题", user_id: "staff-1", brand: "epig", market: "au" },
      () => {},
      controller.signal,
    );
    controller.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(controller.signal.aborted).toBe(true);
  });

  it("sends observable method, market, revision and payload for writes", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(detailFixture()));
    vi.stubGlobal("fetch", fetchMock);
    const api = new StaffApi("memory-only-token");
    const signal = new AbortController().signal;

    await api.listCases("sg", {
      status: "waiting_approval",
      request_type: "damaged",
      limit: 20,
      offset: 40,
    }, signal);

    await api.createDraft("sg", 9, {
      expected_revision: 12,
      action: "refund",
      internal_summary: "摘要",
      consumer_reply: "回复",
      evidence_chunk_ids: ["chunk-1"],
    }, signal);

    await api.submitDraft("sg", 9, 3, 12, signal);
    await api.suggest("sg", 9, 12, signal);
    await api.approve("sg", 9, "approval-1", 12, signal);
    await api.reject("sg", 9, "approval-1", 12, "内部原因", signal);

    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>;
    const [listUrl] = calls[0]!;
    expect(listUrl).toContain("/api/v1/after-sales/sg/cases?");
    expect(listUrl).toContain("status=waiting_approval");
    expect(listUrl).toContain("request_type=damaged");
    expect(listUrl).toContain("offset=40");
    expect(calls[0]![1].method).toBe("GET");

    const [draftUrl, draftInit] = calls[1]!;
    expect(draftUrl).toBe("/api/v1/after-sales/sg/cases/9/drafts");
    expect(draftInit.method).toBe("POST");
    expect((draftInit.headers as Record<string, string>).Authorization).toBe("Bearer memory-only-token");
    expect(JSON.parse(String(draftInit.body))).toMatchObject({
      expected_revision: 12,
      evidence_chunk_ids: ["chunk-1"],
    });

    const [submitUrl, submitInit] = calls[2]!;
    expect(submitUrl).toBe("/api/v1/after-sales/sg/cases/9/drafts/3/submit");
    expect(JSON.parse(String(submitInit.body))).toEqual({ expected_revision: 12 });

    const [suggestUrl, suggestInit] = calls[3]!;
    expect(suggestUrl).toBe("/api/v1/after-sales/sg/cases/9/suggestion");
    expect(JSON.parse(String(suggestInit.body))).toEqual({ expected_revision: 12 });

    const [approveUrl, approveInit] = calls[4]!;
    expect(approveUrl).toBe("/api/v1/after-sales/sg/cases/9/approvals/approval-1/approve");
    expect(JSON.parse(String(approveInit.body))).toEqual({ expected_revision: 12 });

    const [rejectUrl, rejectInit] = calls[5]!;
    expect(rejectUrl).toBe("/api/v1/after-sales/sg/cases/9/approvals/approval-1/reject");
    expect(JSON.parse(String(rejectInit.body))).toEqual({ expected_revision: 12, reason: "内部原因" });
  });
});
