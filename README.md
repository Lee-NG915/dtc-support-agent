# DTC Support Agent｜电商售后智能工作台

面向电商客服与售后场景的可运行 Agent 系统，包含两套应用：**消费者对话工作台**（订单咨询、物流查询、退换货政策解答、写操作人工确认）和**员工售后审批后台**（证据核验、AI 建议、版本草稿、持久审批、Worker 执行与回执对账）。

本仓库是 [Adaptive-rag](https://github.com/JEen-wang/Adaptive-rag) 的二次开发 fork，保留 [MIT 许可与原始版权声明](LICENSE)。当前实现与上游的差异见[项目方案](docs/project-plan.md)。

## 当前实现

### 消费者对话工作台（`/`）

| 能力 | 实现入口 |
|---|---|
| 意图识别与五路复杂度路由（DIRECT / SINGLE / MULTI / AGENT / REFUSE） | `app/agents/graph.py`、`app/agents/nodes/` |
| BM25 + 向量混合检索、RRF 融合、Cross-Encoder 精排、Self-RAG 多跳 | `app/retrieval/` |
| 订单 / 物流 / 退款工具调用，服务端归属校验与风险分级 | `app/tools/` |
| 高风险写操作先返回 `pending_action`，用户确认后才执行 | `app/services/chat_service.py` |
| 忠实度检查与自纠错，核验失败时 fail-closed 展示证据原文 | `app/agents/nodes/faithfulness.py` |
| SSE 流式对话、引用展示、确认卡片 | `frontend/src/` |

### 员工售后审批后台（`/staff`）

| 能力 | 实现入口 |
|---|---|
| 员工身份、角色（support / approver / auditor / operator）与市场隔离 | `app/repositories/staff_repository.py` |
| 案件同步、证据材料、人工核验记录 | `app/services/after_sales/service.py` |
| AI 建议生成（严格输出校验，金额与引用白名单 fail-closed） | 同上 |
| 版本化草稿、持久审批、修改后旧审批失效 | `app/repositories/models.py` |
| Worker 租约执行、幂等键、有限重试、回执核验与人工对账 | `app/services/after_sales/execution.py`、`app/worker/` |
| 摘要优先的员工页面、角色驱动的工作流 | `frontend/src/staff/` |

跨仓库 E-pig 对接契约（OpenAPI + 共享 fixtures + SHA-256 vendor 包）见[对接规范](docs/architecture/modules/after-sales/epig-contract.md)。未接入 E-pig 时可使用内置 Fake Connector 演示完整流程。

## 快速开始（本地）

要求 Python 3.11+（验证过 3.12）、Node 20+；可选 `DEEPSEEK_API_KEY`（留空则进入离线演示模式，展示检索原文与可核对的业务状态）。

```bash
git clone https://github.com/Lee-NG915/dtc-support-agent.git
cd dtc-support-agent
uv sync --frozen            # 或 python -m venv .venv && pip install -e ".[dev]"
cp .env.example .env        # 填入 DEEPSEEK_API_KEY（可选）
uv run python -m scripts.seed_demo
uv run uvicorn app.asgi:app --port 8002

# 另开终端：前端
cd frontend && npm ci && npm run dev   # http://127.0.0.1:5173
```

第一条消息：

```bash
curl -s localhost:8002/api/v1/chat \
  -H 'content-type: application/json' \
  -d '{"message":"取消订单ORD10003","user_id":"u_demo"}'
# 响应包含 pending_action；携带 session_id 与 confirm_action_id 再次请求才会真正执行
```

演示订单：`ORD10001`（已发货）、`ORD10002`（已签收定制杯）、`ORD10003`（已支付未发货）。详见[快速开始](docs/02-getting-started.md)。

## 启用员工售后后台

消费者演示与员工后台使用不同身份模型，建议以**独立实例**运行（独立数据库与端口）：

```bash
# 全新 SQLite 库初始化合成员工（仅 development/testing）
uv run python -m scripts.seed_after_sales_staff \
  --database-url sqlite+aiosqlite:///./data/local/staff.db
ALEMBIC_DATABASE_URL=sqlite+aiosqlite:///./data/local/staff.db uv run alembic stamp head

# 关键环境变量
AFTER_SALES_ENABLED=true
BUSINESS_CONNECTOR=fake                     # 或配置 E-pig 地址与 service token
DATABASE_URL=sqlite+aiosqlite:///./data/local/staff.db
AFTER_SALES_DEMO_LOGIN_ENABLED=true          # 仅非生产；演示密码换取短时签名会话
AFTER_SALES_DEMO_PASSWORD=<至少8位>
AFTER_SALES_DEMO_SESSION_SECRET=<至少32位随机串>
AFTER_SALES_SKU_CATEGORY_MAP={"SYNTH-MUG-001":"homewares"}

uv run uvicorn app.asgi:app --port 8003      # 员工 API，页面入口 /staff
uv run python -m app.worker                  # 独立 Worker 进程，执行已批准的工单
```

完整业务流程：同步案件 → 人工核验材料 → AI 建议 / 草稿 → 提交审批 → 审批人批准 → Worker 建单 → 回执核验。角色、市场与接口见[流程设计](docs/architecture/modules/after-sales/dtc-workflow.md)。

## 部署

- 本地 / 单机：上述 uv / pip 方式配合 systemd 即可运行；前端 `npm run build` 后由 FastAPI 直接托管，无需独立前端服务器。
- 反向代理建议统一加访问控制（如 Caddy basic auth 或私网）；消费者演示的 `user_id` 为客户端提交，**不要无认证暴露到公网**。
- Docker Compose（演示 / 售后 / 生产候选）与生产试点方案见[部署与运维](docs/11-deployment.md)。
- 使用 `deepseek-flash` 等带思考模式的模型时，建议设置 `DEEPSEEK_THINKING_MODE=disabled`，避免思考过程耗尽 `max_tokens` 导致结构化输出为空、触发 fail-closed 兜底。

## 验证与评测口径

- 工程测试：`uv run pytest -q`；前端 `npm --prefix frontend run test && npm --prefix frontend run build`。
- 文档结构检查：`python3 scripts/check_project_docs.py`。
- 售后固定评测：`uv run python -m eval.runner --task all`（默认离线；真实模型需显式配置与预算）。revision7 后 v4 真实模型保留集 H01–H08 满足冻结断言、工程守卫 4/4；80/40 完整集与独立 Faithfulness 固定集尚未建成。
- 各项验收的实际范围与失败保留见[流程记录](docs/records/after-sales/dtc-workflow.md)与[项目档案](.dtc/target-profile.md)；上游历史指标（`eval/MEASURED.md`）不作为本 fork 成绩。

## 文档入口

- [项目背景与实施方案](docs/project-plan.md) · [技术文档目录](docs/README.md)
- [学习路线](docs/learning/README.md)：从前端经验理解后端、Agent 与部署
- [Issue #2：售后开发与验收](https://github.com/Lee-NG915/dtc-support-agent/issues/2) · [Issue #1：原方案规划](https://github.com/Lee-NG915/dtc-support-agent/issues/1)
- [文档规范](docs/guides/documentation.md) · [AI 协作规范](docs/guides/ai-collaboration.md)

## 使用边界

- 政策知识库为内部自编演示条款，非官方政策；订单与物流为合成数据或 Fake Connector，未连接真实店铺、承运商或支付网关。
- 首期不包含真实支付退款、自动取消真实订单等外部写操作；消费者自动回复入口不属于员工售后后台范围。
- 生产试点（Oracle + Tailscale / E-pig Netlify + Neon）尚未完成部署；真实用户试用、容量与生产 SLA 未验收。
- 员工端固定密码演示登录仅限 development/testing，生产启动会被拒绝。

## 参与贡献

修改前建议阅读[贡献指南](CONTRIBUTING.md)与 [AI 协作规范](docs/guides/ai-collaboration.md)。提交前运行 Ruff、mypy、pytest 与前端测试；请勿提交 `.env`、密钥、本地数据库或生成的评测报告。

## 许可证

[MIT License](LICENSE)。基于 Adaptive-rag 二次开发，保留原始许可与版权声明。
