# 项目档案

核对日期：2026-11-06。范围：开发助手接入、文档维护与隔离开发环境。后续发生源码或命令变化时更新本文件。

## 项目定位与代码事实

业务范围、现状与拟实现能力只在 [项目方案](../docs/project-plan.md) 维护。本文件负责给开发助手定位实际工具与代码，不复制全部架构。

| 入口 | 当前作用 |
|---|---|
| `pyproject.toml` / `uv.lock` | Python ≥3.11，依赖与锁定版本 |
| `app/api/routes/chat.py` / `app/schemas/chat.py` | `/chat` 与流式接口、请求响应校验 |
| `app/services/chat_service.py` | ChatService 会话与确认流程 |
| `app/agents/graph.py` | LangGraph 构建与业务节点连接 |
| `app/tools/` / `app/repositories/` | 工具调用、权限规则、持久数据访问 |
| `app/skills/` | 应用业务技能配置；不加载本次开发助手技能 |
| `frontend/package.json` | React/TypeScript/Vite/Vitest 脚本 |
| `alembic/` / `docker-compose.yml` | 既有环境定义；迁移0005与独立Worker已实现，独立DTC Compose已验证，两仓Compose候选另验 |
| `tests/` / `eval/` | 工程测试与模型评测 |
| `.agents/skills/` | 本仓开发助手技能正本，随分支版本管理 |

具体函数和行为使用前再读当前源码。不要根据目录名推断真实身份、持久审批或可恢复 Worker 已经完成。

## 命令入口

以下命令根据仓库配置核对；列出命令不代表本次全部执行成功。请在仓库根目录运行。

| 用途 | 命令与前提 |
|---|---|
| 文档/技能结构 | `python3 scripts/check_project_docs.py`；仅标准库，无模型或数据库依赖 |
| 补丁空白检查 | `git diff --check` |
| 安装 Python 开发依赖 | `uv sync --extra dev`；需可用 Python 与依赖下载条件 |
| 锁文件检查 | `uv lock --check` |
| 格式与静态检查 | 安装开发依赖后 `uv run ruff format --check .`、`uv run ruff check .`、`uv run mypy` |
| 工程测试 | 安装开发依赖后 `uv run pytest -q`；先按受影响测试选择范围 |
| 前端依赖 | `npm ci --prefix frontend` |
| 前端测试与构建 | `npm --prefix frontend run test`、`npm --prefix frontend run build` |
| 应用启动与依赖配置 | 按 [现有快速开始](../docs/02-getting-started.md)；不要编造尚不存在的 Worker 命令 |
| 部署 | 读 [现有部署文档](../docs/11-deployment.md) 与当前 Compose；变更前核对目标环境和授权 |

CI 的触发条件和门槛见 [工作流](../.github/workflows/ci.yml)。当前工作流定义已包含文档、契约、政策检查和Python3.11/3.12测试。2026-11-06已补当前codex/2-epig-after-sales分支push触发，保留main/PR/dispatch及只读权限，不含部署。此前dispatch因默认分支未登记返回404，草稿PR#4也未产生run；本次push是否实际执行与结果必须回读，不能仅凭配置写CI通过。

## 文档与交付入口

- [AGENTS.md](../AGENTS.md)：执行路由。
- [文档规范](../docs/guides/documentation.md)：归属、写作、状态和学习要求。
- [学习路线](../docs/learning/README.md)：用户从现有前端经验进入后端/Agent 的路径。
- [技能指南](../docs/guides/skills.md)：选择依据、调用与更新。
- [接入记录](../docs/records/workflows/skills-and-docs.md)：实际验证与边界。
- [Issue #1](https://github.com/Lee-NG915/dtc-support-agent/issues/1)：规划上下文；已有关联分支 `codex/1-project-plan`，操作前仍需核对实际 Git 状态。

仓库远端为 `Lee-NG915/dtc-support-agent`。这是项目身份信息，不是合并、部署、访问真实店铺或发送消费者消息的授权。

## 本机环境恢复证据（2026-11-06）

本轮已安装uv与Python3.12，并用`uv sync --frozen --extra dev --python 3.12`按原锁文件建立环境。锁检查、Ruff格式/静态、mypy125源文件和pytest242项通过；依赖4条警告保留。命令可用是本轮结果，不等于将来环境自动满足。该历史环境恢复阶段未运行真实模型；当前专用DeepSeek评测授权见协作规范，工程测试仍默认Fake/离线。

Docker已启动，Postgres16官方镜像用于专用隔离测试；既有默认Compose保留，独立售后Compose已验证PG/迁移/API/Worker。E-pig已通过恢复真实技能源解决agent-doctor失败；本仓技能仍自包含，不依赖该软链。持续授权与审批边界见协作规范。


DTC-2a增量（2026-11-06，本地验收）：新增`app/connectors/`、staff repository与`/api/v1/after-sales`只读路由，`AFTER_SALES_ENABLED`缺省false；启用后员工Bearer认证也保护旧chat。迁移入口不读取.env，显式设置`ALEMBIC_DATABASE_URL`后运行`uv run alembic upgrade head`；PG启动不create_all。合成员工工具`uv run python -m scripts.seed_after_sales_staff --database-url <新的本地SQLite URL>`只作用新库，凭据默认写忽略目录0600新文件，不打印。命令细节和证据见[流程设计](../docs/architecture/modules/after-sales/dtc-workflow.md)与[实施记录](../docs/records/after-sales/dtc-workflow.md)；该2a范围不含审批/Worker；当前2b状态见下节。


DTC-2b增量（2026-11-06，本地验收）：`app/services/after_sales/`、after_sales_repository和after_sales_workflow routes提供持久同步、材料/证据、版本草稿、审批及prepared操作；迁移0004新增业务表。`POST /api/v1/after-sales/{market}/cases/sync`为support入口，批准不调用外部create。Worker/租约执行已完成2c核心本地范围验收，员工页面仍待后续章节；实际验收分层见流程记录，不能把prepared称为已建单。

DTC-2c核心增量（2026-11-06，本地范围验收）：`app/services/after_sales/execution.py`及`python -m app.worker`提供有界feed轮询、租约/epoch派遣、原键回执核验、有限重试与人工对账；0005补操作恢复字段。`uv run python scripts/run_after_sales_pg.py`在全新专用合成PG16容器执行固定测试并清理，不读取.env/继承目标；真实PG与SQLite迁移分列。`uv run python -m app.worker --help`可查看参数，真正派遣前必须核对专用环境和显式迁移。当前结果不等于Compose、跨仓HTTP、浏览器或生产验收；详见流程设计/记录。


员工后端增量（2026-11-06，本地范围验收）：case的`evidence`/`ticket`只读与`suggestion`草稿入口已实现；ApprovalView提供已存冻结snapshot白名单。生成前后与真实保存事务验证员工/源指纹/UTC日期，仅保存editing；OfflineLLM明确不可用。共享`verification.py`供Worker与只读工单核验复用。专用PG入口现包含workflow/execution/workbench，仍不代表员工页面、真实模型质量或整体验收。


员工前端工程增量（2026-11-06，实际浏览器待验）：`frontend/src/staff/`与`/staff`独立员工入口已实现，token/业务输入仅内存；复用现有React/CSS/lucide，仅增jsdom开发测试依赖。构建dist存在时API显式返回/staff shell，不对未知API做SPA回退；无dist仍API-only。工程检查与浏览器/完整旅程分列，见流程记录。

评测安全入口增量（2026-11-06，本地已验证、待交付）：默认`uv run python -m eval.runner --task all`显式离线，all不包含epig-policy；后者单独执行。真实模型需专用DTC_EVAL配置、显式允许及cases/calls上限，当前真实DeepSeek评测已有专用授权，工程默认仍离线。完整门禁`uv run pytest -q --cov=app --cov=eval --cov-report=term:skip-covered --cov-fail-under=70`本地535通过/4跳过/80.89%。CI补前端Node24与独立PG入口，定义存在不等于远端已运行。详见[评测指南](../docs/10-evaluation.md)。

部署/工具轨迹候选（2026-11-06，最终门禁进行中）：`docker-compose.after-sales.yml`新增独立PG/migrate/API/Worker；须显式`--env-file`及随机新项目名，PG无主机端口，API loopback，Fake仅启动验证。Dockerfile按uv.lock frozen安装并包含迁移；镜像保留production与禁本地模型默认，指南见部署文档。eval/e2e新增真实Tool.run记录与可选工具期望；脱敏/截断不可评分，正向与抑制分别计数。`after_sales_holdout_v1`12例已准备未运行，完整业务质量未测。

当前验收进度（2026-11-06）：六故事服务层与跨系统丢响应备份恢复已独立验证；两仓Compose空feed独立通过。真实模型保留集修订夹具后已执行，生成6例4例全满足，金额单位等质量缺口保留；独立Chromium页面与清理修复仍在验收。最新有效计数与失败保留在[流程记录](../docs/records/after-sales/dtc-workflow.md)，不以本档案历史计数代替当前成绩。
