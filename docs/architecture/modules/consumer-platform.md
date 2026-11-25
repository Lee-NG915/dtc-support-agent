# 消费者平台：GitHub 认证与订单自助查询设计

建立日期：2026-11-25。状态：已确认方向，阶段 1 实施中。本文是 DTC 消费者平台（`/`）的产品与技术设计入口；员工售后后台（`/staff`）的职责不变，见[售后流程设计](after-sales/dtc-workflow.md)。

## 1. 产品视角与决策（2026-11-25 用户确认）

- `http://<host>/` 正式定义为**面向消费者的 DTC Support Agent**：消费者登录后查询**本人**订单、订单详情、物流与售后进度，并可使用 AI 对话。
- 用户认证采用 **GitHub OAuth**。DTC 与 E-pig 以 `github_id` 作为统一消费者主体，推翻产品蓝图中"不为售后另建消费者账号"的旧非目标；消费者诉求仍按既有归属规则处理，不扩大为自动售后审批。
- 员工后台继续使用既有 staff 身份体系，不引入 GitHub 登录。
- 阶段 1（本次）：DTC 完成 GitHub 登录、消费者数据 API 与页面、AI 对话主体化；数据经 **ConsumerConnector** 提供，默认 Fake（合成数据），接口形态与阶段 2 一致。
- 阶段 2（后续）：E-pig 接入 GitHub 登录并建立 `github_id ↔ cart_id` 映射，新增按消费者主体列出订单的服务接口（走契约正本流程），DTC 切换为真实数据。

## 2. 角色与数据归属

| 角色 | 能做什么 | 不能做什么 |
|---|---|---|
| 消费者（GitHub 主体） | 查看映射到本人的订单/物流/售后；使用本人身份的 AI 对话与确认 | 查看他人数据；持有 service token；直接写售后单 |
| DTC 服务（BFF） | 校验 OAuth 会话；代查消费者数据；保存映射与处理所需快照 | 信任客户端提交的用户编号；把 E-pig token 下发浏览器 |
| 员工（staff 体系） | 既有售后工作台操作 | 用消费者身份访问员工接口 |

- `github_id`（GitHub 数字用户 ID）是唯一主体键；`login` 仅展示用，不作为授权依据。
- 阶段 1 的 Fake 映射为显式演示配置：指定演示 GitHub 账号 → 合成订单集合；未映射账号登录后看到空数据与说明，而不是他人数据。

## 3. 认证流程

授权码流程：`GET /api/v1/auth/github/login`（生成并存储 state）→ GitHub 授权 → `GET /api/v1/auth/github/callback`（校验 state、换取 token、读取用户）→ 签发 DTC 会话。

- 配置（计划，均从环境读取）：`GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、`GITHUB_OAUTH_REDIRECT_URL`、`CONSUMER_SESSION_SECRET`（≥32 位）。
- state 一次性、短时效，防 CSRF；会话为 HMAC 签名 token，含 `github_id`、签发与过期时间，默认 8 小时；注销即清除。
- 会话 cookie：`HttpOnly`、`SameSite=Lax`；HTTPS 就绪后加 `Secure`。生产必须 HTTPS。
- 未登录访问消费者数据接口返回 401；`/api/v1/chat` 在消费者平台模式下要求有效会话，员工实例仍走 staff 校验。

## 4. 消费者数据接口（计划）

| 接口 | 行为 |
|---|---|
| `GET /api/v1/consumer/me` | 当前 GitHub 用户与映射状态 |
| `GET /api/v1/consumer/orders` | 本人订单列表（市场、状态、金额、时间） |
| `GET /api/v1/consumer/orders/{order_id}` | 本人订单详情；非本人返回 404 |
| `GET /api/v1/consumer/orders/{order_id}/shipment` | 本人订单物流轨迹 |
| `GET /api/v1/consumer/after-sales` | 本人售后诉求与工单进度 |

- 归属校验在服务端按会话 `github_id` 强制过滤；路径中的订单号只用于定位，不参与授权。
- ConsumerConnector（计划接口）：`list_orders(github_id)`、`get_order(github_id, order_id)`、`get_shipment(...)`、`list_after_sales(...)`。Fake 实现返回合成数据；E-pig 实现走 service token + 主体参数（阶段 2 契约新增）。

## 5. AI 对话主体化

- 消费者模式下，`user_id` 由会话映射为稳定内部主体（如 `gh_<github_id>`），不再接受请求体中的 `user_id`；工具查询继续走服务端归属校验。
- HITL 语义不变：高风险写操作先展示方案、等待本人确认；演示写操作只作用于演示数据。
- 未登录仅可使用公开政策问答（可选配置），涉及订单一律先登录。

## 6. 失败与兼容

- GitHub 回调 state 不符、token 交换失败、会话过期：返回明确错误并引导重新登录，不降级为匿名可查。
- E-pig 不可达（阶段 2）：数据页显示来源不可用并允许重试，不展示陈旧快照冒充实时。
- 员工后台、既有员工 API、契约 v1 行为不变；消费者接口为新增前缀，不修改既有路径。

## 7. 验证场景（实现后逐项执行）

1. 未登录访问消费者接口 → 401；员工接口不受影响。
2. OAuth 全流程登录 → `/consumer/me` 返回 GitHub 用户；state 重放/篡改被拒。
3. 映射账号看到本人订单/物流/售后；未映射账号为空；跨主体访问订单详情为 404。
4. AI 对话在会话主体内查询与确认；伪造 `user_id` 不改变数据范围。
5. 注销后会话失效；过期会话返回 401。

## 8. 待决项

- GitHub OAuth App 的 client_id/secret 由用户创建并提供（回调地址随域名/HTTPS 方案调整）。
- 阶段 2 的 E-pig 映射存储与订单列表契约，需在 E-pig 仓另行设计评审，并同步蓝图与 DTC#2 / E-pig#2。
