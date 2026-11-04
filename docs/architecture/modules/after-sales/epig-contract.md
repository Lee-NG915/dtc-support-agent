# E-pig 售后对接契约（计划）

更新日期：2026-11-04。状态：计划，所有接口均未实现；字段以双方评审后的版本为准。产品背景见 [产品形态与用户旅程](product-blueprint.md)。

## 系统角色与边界

E-pig 是独立的 Next.js 电商前台（多市场 au/sg/us，已有订单路由 `/api/[market]/orders`、订单详情页与 postgres adapter）。本系统是售后中台：客服工作台、政策检索、审批与工单流转。

- 两边通过 HTTP API 协作，**不共享数据库**；E-pig 的存储选型对中台透明。
- 消费者诉求与工单的业务数据由 E-pig 持有；中台保存处理过程、审批与操作账本。
- 首期不做消费者 AI 自动回复、真实退款与自动取消订单。

```text
消费者 → E-pig（订单/售后入口） ←HTTP→ 售后中台（本系统） ← 客服 / 审批人
```

## 鉴权与店铺（市场）权限

- 服务间调用使用服务端 token（计划：`AFTER_SALES_SERVICE_TOKEN`，只存服务端环境变量），消费者浏览器不持有该 token。
- E-pig 的 market 对应中台的"店铺"维度：客服账号绑定允许访问的市场列表，跨市场查询必须拒绝。这对应项目验收"跨店铺访问不能发生"。
- 消费者侧请求由 E-pig 校验登录态与订单归属；中台不信任任何请求体里的 `user_id`。

## 接口清单（计划）

以下路径以 E-pig 现有 `/api/[market]/...` 约定为基准，实现时可调整，但语义保持不变。

### 1. 订单查询（E-pig 提供，中台调用）

`GET /api/{market}/orders/{orderId}`

已有路由，需要核对并扩充为服务视图字段：`order_id`、`market`、`status`、`items[]`（商品、数量、单价）、`amount`、`currency`、`created_at`、`shipping_address`（脱敏）。

### 2. 物流查询（E-pig 提供，计划新增）

`GET /api/{market}/orders/{orderId}/shipment`

返回 `status`、`events[]`（时间、节点、描述）。无真实承运商时由 mock adapter 提供合成轨迹。

### 3. 消费者诉求提交（E-pig 提供，形态 A）

`POST /api/{market}/after-sales/requests`

入参：`order_id`、`type`（破损 / 错发 / 退货 / 其他）、`description`、`attachments[]`。
E-pig 校验登录态与订单归属后创建诉求单，返回 `request_id`、`status=pending`。

### 4. 诉求队列查询（E-pig 提供，中台调用）

`GET /api/{market}/after-sales/requests?status=pending&cursor=...`

中台轮询拉取，用于工作台诉求队列；游标防止重复与遗漏。

### 5. 工单创建（E-pig 提供，中台审批后调用）

`POST /api/{market}/after-sales/tickets`

入参：`request_id`、`action`（补发 / 退货 / 退款 / 其他）、`reply`（给消费者的回复）、`draft_version`、`idempotency_key`。
返回：`ticket_id`、`status`、`receipt`（回执）。

写约定：

- **幂等**：同一 `idempotency_key` 重复提交返回原工单；同键不同参数返回 `conflict`。
- **草稿版本绑定**：`draft_version` 与审批时一致才允许创建；订单或政策版本变化需重新检查。
- **超时与未知结果**：中台超时后先按 `idempotency_key` 查询是否已创建，不盲目重发；无法判定的进入人工对账。

### 6. 工单查询与回复同步（E-pig 提供）

`GET /api/{market}/after-sales/tickets/{ticketId}`（回执核验、消费者进度展示）
工单状态与客服回复由 E-pig 在订单详情页展示；中台不直接向消费者推送消息。

### 7. 只读政策问答（中台提供，形态 B，后续）

`POST /api/v1/chat`（现有接口，计划增加消费者只读通道）

仅允许政策检索路径，禁用业务工具与写操作，独立限流。E-pig 聊天组件经服务端转发调用，不暴露中台内网地址。

## 错误码约定

`unauthorized`（服务 token 无效）、`forbidden_market`（跨市场）、`not_found`（订单 / 诉求 / 工单）、`conflict`（幂等键冲突或版本变化）、`rate_limited`、`upstream`（下游不可用）。中台按类型决定重试、拒答或转人工。

## 故障演练场景（M4 验收对应）

1. 客服双击创建工单、网络重试：E-pig 只产生一张工单。
2. 中台在"E-pig 已建单、回执未收到"时崩溃：恢复后按幂等键查到同一回执。
3. 审批后草稿版本变化、审批过期：创建被拒绝。
4. 客服查询无权限市场的订单：`forbidden_market`。
5. E-pig 不可用：中台明确降级提示，不伪造订单或工单结果。

## 两边任务拆解

E-pig 仓库（方案见该仓库 `docs/engineering/after-sales-agent-integration.md`）：

- [ ] after-sales 域：contracts、mock/postgres adapter、Route Handlers
- [ ] 订单详情页售后入口与工单进度区
- [ ] 服务 token 校验、市场隔离、消费者订单归属校验与测试

本仓库：

- [ ] `EpigConnector`（HTTP、超时、有限重试、幂等键、回执核验），`BUSINESS_CONNECTOR=fake/epig` 配置切换，Fake 保留用于离线开发
- [ ] 诉求队列 / 工单详情 / 审批页面与工单状态机
- [ ] 持久化审批与操作账本，联调故事线与故障演练记录

## 待评审的开放问题

- 诉求与工单的附件（照片）存储方式与大小限制。
- 诉求队列用中台轮询还是 E-pig  webhook 推送；首期倾向轮询，减少外部回调鉴权面。
- 消费者回复是否支持多轮追加，还是首期一次性结论。
