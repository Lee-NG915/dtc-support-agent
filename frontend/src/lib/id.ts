/** Generate ids in all browser contexts; randomUUID requires a secure context (HTTPS or localhost). */
export function newId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  globalThis.crypto?.getRandomValues?.(bytes);
  const random = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `id-${Date.now().toString(36)}-${random || Math.random().toString(36).slice(2, 18)}`;
}
