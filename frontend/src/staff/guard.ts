import { isSessionEndingError } from "./api";

export type RunResult<T> =
  | { ok: true; stale: false; value: T }
  | { ok: false; stale: true }
  | { ok: false; stale: false; error: unknown };

function isAbort(error: unknown): boolean {
  return (
    (error instanceof DOMException && error.name === "AbortError") ||
    (typeof error === "object" && error !== null && "name" in error &&
      (error as { name?: unknown }).name === "AbortError")
  );
}

/**
 * One generation belongs to one authenticated identity + market + workspace.
 * Bumping it aborts every in-flight request. A late success or 403 from an old
 * generation can never write back or sign out the newer identity.
 */
export class RequestGuard {
  private generationValue = 0;
  private readonly controllers = new Set<AbortController>();

  get generation(): number {
    return this.generationValue;
  }

  isCurrent(generation: number): boolean {
    return generation === this.generationValue;
  }

  bump(): number {
    this.generationValue += 1;
    this.abortAll();
    return this.generationValue;
  }

  abortAll(): void {
    for (const controller of this.controllers) controller.abort();
    this.controllers.clear();
  }

  async runWithAuth<T>(
    generation: number,
    work: (signal: AbortSignal) => Promise<T>,
    onSessionEnding: (generation: number, error: unknown) => void,
  ): Promise<RunResult<T>> {
    if (!this.isCurrent(generation)) return { ok: false, stale: true };
    const controller = new AbortController();
    this.controllers.add(controller);
    try {
      const value = await work(controller.signal);
      if (!this.isCurrent(generation) || controller.signal.aborted) {
        return { ok: false, stale: true };
      }
      return { ok: true, stale: false, value };
    } catch (error) {
      if (!this.isCurrent(generation) || controller.signal.aborted || isAbort(error)) {
        return { ok: false, stale: true };
      }
      if (isSessionEndingError(error)) {
        if (this.isCurrent(generation)) onSessionEnding(generation, error);
        return { ok: false, stale: false, error };
      }
      return { ok: false, stale: false, error };
    } finally {
      this.controllers.delete(controller);
    }
  }
}

/** Object URLs live only for the current case scope and are always revoked. */
export class AttachmentUrlStore {
  private readonly urls = new Map<string, string>();
  private readonly create: (blob: Blob) => string;
  private readonly revoke: (url: string) => void;

  constructor(
    create: (blob: Blob) => string = (blob) => URL.createObjectURL(blob),
    revoke: (url: string) => void = (url) => URL.revokeObjectURL(url),
  ) {
    this.create = create;
    this.revoke = revoke;
  }

  put(key: string, blob: Blob): string {
    const previous = this.urls.get(key);
    if (previous) this.revoke(previous);
    const url = this.create(blob);
    this.urls.set(key, url);
    return url;
  }

  get(key: string): string | undefined {
    return this.urls.get(key);
  }

  has(key: string): boolean {
    return this.urls.has(key);
  }

  revokeAll(): void {
    for (const url of this.urls.values()) this.revoke(url);
    this.urls.clear();
  }
}

export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}
