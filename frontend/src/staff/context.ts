import { createContext, useContext } from "react";
import type { StaffApi } from "./api";
import type { RunResult } from "./guard";
import type { Market, StaffIdentity } from "./types";

export interface StaffContextValue {
  api: StaffApi;
  identity: StaffIdentity;
  market: Market;
  generation: number;
  isCurrent: (generation: number) => boolean;
  run: <T>(work: (api: StaffApi, signal: AbortSignal) => Promise<T>) => Promise<RunResult<T>>;
  reportSessionEnding: (error: unknown, generation: number) => void;
  markRead: (value?: string) => void;
  canSupport: boolean;
  canApprove: boolean;
  canAudit: boolean;
  canOperate: boolean;
}

export const StaffContext = createContext<StaffContextValue | null>(null);

export function useStaff(): StaffContextValue {
  const value = useContext(StaffContext);
  if (value === null) throw new Error("staff context is unavailable");
  return value;
}
