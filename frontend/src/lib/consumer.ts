import type { Order, RefundItem, ShipmentInfo } from "../types";

export interface AuthMe {
  auth_enabled: boolean;
  signed_in: boolean;
  github_id?: string;
  login?: string;
  demo_data_available?: boolean;
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`请求失败（HTTP ${response.status}）`);
  return response.json() as Promise<T>;
}

export function getAuthMe(): Promise<AuthMe> {
  return getJson<AuthMe>("/api/v1/auth/me");
}

export function listOrders(): Promise<{ items: Order[]; total: number }> {
  return getJson("/api/v1/consumer/orders");
}

export function getShipment(orderId: string): Promise<ShipmentInfo> {
  return getJson(`/api/v1/consumer/orders/${encodeURIComponent(orderId)}/shipment`);
}

export function listAfterSales(): Promise<{ items: RefundItem[]; total: number }> {
  return getJson("/api/v1/consumer/after-sales");
}

export async function logout(): Promise<void> {
  await fetch("/api/v1/auth/logout", { method: "POST" });
}
