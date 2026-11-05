import { describe, expect, it, vi } from "vitest";
import { StaffApiError } from "./api";
import { AttachmentUrlStore, RequestGuard } from "./guard";

describe("RequestGuard", () => {
  it("drops a late success from an old identity generation", async () => {
    const guard = new RequestGuard();
    const generation = guard.generation;
    let resolve!: (value: string) => void;
    const pending = new Promise<string>((done) => { resolve = done; });
    const onSessionEnding = vi.fn();

    const run = guard.runWithAuth(generation, () => pending, onSessionEnding);
    guard.bump();
    resolve("late success from the previous identity");

    await expect(run).resolves.toEqual({ ok: false, stale: true });
    expect(onSessionEnding).not.toHaveBeenCalled();
  });

  it("does not let an old identity's late 403 sign out the new identity", async () => {
    const guard = new RequestGuard();
    const generation = guard.generation;
    const onSessionEnding = vi.fn();

    const run = guard.runWithAuth(
      generation,
      async () => {
        await Promise.resolve();
        throw new StaffApiError(403, "旧身份被撤权", "FORBIDDEN_ROLE", null);
      },
      onSessionEnding,
    );
    guard.bump();

    await expect(run).resolves.toEqual({ ok: false, stale: true });
    expect(onSessionEnding).not.toHaveBeenCalled();
  });

  it("reports a current 401/403 but not a self-approval business rejection", async () => {
    const guard = new RequestGuard();
    const generation = guard.generation;
    const onSessionEnding = vi.fn();

    const authFailure = await guard.runWithAuth(
      generation,
      async () => { throw new StaffApiError(401, "unauthorized", "UNAUTHORIZED", "trace-1"); },
      onSessionEnding,
    );
    expect(authFailure.ok).toBe(false);
    expect(onSessionEnding).toHaveBeenCalledWith(generation, expect.any(StaffApiError));

    onSessionEnding.mockClear();
    const selfApproval = await guard.runWithAuth(
      guard.generation,
      async () => {
        throw new StaffApiError(403, "self approval", "SELF_APPROVAL_FORBIDDEN", null);
      },
      onSessionEnding,
    );
    expect(selfApproval.ok).toBe(false);
    expect(onSessionEnding).not.toHaveBeenCalled();
  });
});

describe("AttachmentUrlStore", () => {
  it("revokes replaced object URLs and all URLs on scope cleanup", () => {
    let counter = 0;
    const revoked: string[] = [];
    const store = new AttachmentUrlStore(
      () => `blob:test-${++counter}`,
      (url) => { revoked.push(url); },
    );

    const first = store.put("attachment-1", new Blob(["one"]));
    const second = store.put("attachment-1", new Blob(["two"]));
    store.put("attachment-2", new Blob(["three"]));

    expect(first).toBe("blob:test-1");
    expect(second).toBe("blob:test-2");
    expect(revoked).toEqual(["blob:test-1"]);

    store.revokeAll();
    expect(revoked).toEqual(["blob:test-1", "blob:test-2", "blob:test-3"]);
    expect(store.has("attachment-1")).toBe(false);
  });
});
