import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { getShipment, listAfterSales, listOrders } from "../lib/consumer";
import type { Order, RefundItem, ShipmentInfo } from "../types";

const STATUS_LABELS: Record<string, string> = {
  created: "待支付",
  paid: "已支付",
  shipped: "已发货",
  delivered: "已签收",
  cancelled: "已取消",
  refunding: "退款中",
  refunded: "已退款",
};

const EVENT_LABELS: Record<string, string> = {
  warehouse: "仓库处理中",
  in_transit: "运输中",
  delivered: "已签收",
};

function money(cents: number): string {
  return `¥${(cents / 100).toFixed(2)}`;
}

function dateTime(value: string | null): string {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

export function AccountPanel({ login, onClose }: { login: string; onClose: () => void }) {
  const [tab, setTab] = useState<"orders" | "afterSales">("orders");
  const [orders, setOrders] = useState<Order[]>([]);
  const [refunds, setRefunds] = useState<RefundItem[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [shipment, setShipment] = useState<ShipmentInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([listOrders(), listAfterSales()])
      .then(([orderData, refundData]) => {
        setOrders(orderData.items);
        setRefunds(refundData.items);
      })
      .catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "加载失败"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selected) {
      setShipment(null);
      return;
    }
    getShipment(selected)
      .then(setShipment)
      .catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "物流加载失败"));
  }, [selected]);

  const selectedOrder = orders.find((order) => order.order_id === selected) ?? null;

  return (
    <div className="dialog-backdrop" onMouseDown={onClose}>
      <section
        className="search-dialog account-panel"
        role="dialog"
        aria-modal="true"
        aria-label="我的数据"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="account-header">
          <strong>我的数据</strong>
          <span className="account-login">@{login}</span>
          <button className="icon-button" onClick={onClose} aria-label="关闭我的数据">
            <X size={18} />
          </button>
        </div>
        <div className="account-tabs">
          <button className={tab === "orders" ? "active" : ""} onClick={() => setTab("orders")}>
            订单与物流（{orders.length}）
          </button>
          <button className={tab === "afterSales" ? "active" : ""} onClick={() => setTab("afterSales")}>
            售后（{refunds.length}）
          </button>
        </div>
        <div className="account-body">
          {loading && <p className="account-hint">加载中…</p>}
          {error && <p className="account-hint error">{error}</p>}
          {!loading && !error && tab === "orders" && orders.length === 0 && (
            <p className="account-hint">当前账号暂无可见订单（演示数据仅对指定 GitHub 账号开放）。</p>
          )}
          {!loading && !error && tab === "afterSales" && refunds.length === 0 && (
            <p className="account-hint">暂无售后记录。</p>
          )}
          {tab === "orders" &&
            orders.map((order) => (
              <div key={order.order_id} className="order-block">
                <button
                  className={`order-row${selected === order.order_id ? " expanded" : ""}`}
                  onClick={() => setSelected(selected === order.order_id ? null : order.order_id)}
                >
                  <span className="order-id">{order.order_id}</span>
                  <span className="order-status">{STATUS_LABELS[order.status] ?? order.status}</span>
                  <span className="order-amount">{money(order.total_cents)}</span>
                  <span className="order-date">{dateTime(order.created_at)}</span>
                </button>
                {selectedOrder?.order_id === order.order_id && (
                  <div className="order-detail">
                    <div className="order-items">
                      {order.items.map((item) => (
                        <div key={item.sku} className="order-item-line">
                          <span>
                            {item.name}
                            {item.customized ? "（定制）" : ""} × {item.quantity}
                          </span>
                          <span>{money(item.unit_price_cents)}</span>
                        </div>
                      ))}
                    </div>
                    <div className="order-meta">
                      <span>运单号：{order.tracking_no ?? "—"}</span>
                      <span>收货城市：{order.receiver_city ?? "—"}</span>
                      <span>支付：{dateTime(order.paid_at)}</span>
                      <span>发货：{dateTime(order.shipped_at)}</span>
                      <span>签收：{dateTime(order.delivered_at)}</span>
                    </div>
                    <div className="shipment-events">
                      <strong>物流轨迹</strong>
                      {shipment === null && <p className="account-hint">物流加载中…</p>}
                      {shipment?.events.map((event, index) => (
                        <div key={index} className="shipment-event">
                          <span className="event-status">{EVENT_LABELS[event.status] ?? event.status}</span>
                          <span>{dateTime(event.at)}</span>
                          {event.tracking_no && <span>{event.tracking_no}</span>}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ))}
          {tab === "afterSales" &&
            refunds.map((refund) => (
              <div key={refund.refund_id} className="order-row static">
                <span className="order-id">{refund.refund_id}</span>
                <span className="order-status">{refund.status}</span>
                <span className="order-amount">{money(refund.amount_cents)}</span>
                <span className="order-date">{dateTime(refund.created_at)}</span>
                <span className="refund-reason">
                  {refund.order_id} · {refund.reason}
                </span>
              </div>
            ))}
        </div>
      </section>
    </div>
  );
}
