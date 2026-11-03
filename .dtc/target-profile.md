# 项目档案

核对日期：2026-11-03。范围：开发助手接入与文档维护。后续发生源码或命令变化时更新本文件。

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
| `alembic/` / `docker-compose.yml` | 既有迁移与环境定义；新增 Worker 仍在方案中 |
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

CI 的触发条件和门槛见 [工作流](../.github/workflows/ci.yml)。当前工作流没有调用文档检查器；文档检查是本地交付步骤。开发分支 push 也不一定触发 CI，应读取实际运行状态。

## 文档与交付入口

- [AGENTS.md](../AGENTS.md)：执行路由。
- [文档规范](../docs/guides/documentation.md)：归属、写作、状态和学习要求。
- [学习路线](../docs/learning/README.md)：用户从现有前端经验进入后端/Agent 的路径。
- [技能指南](../docs/guides/skills.md)：选择依据、调用与更新。
- [接入记录](../docs/records/workflows/skills-and-docs.md)：实际验证与边界。
- [Issue #1](https://github.com/Lee-NG915/dtc-support-agent/issues/1)：规划上下文；已有关联分支 `codex/1-project-plan`，操作前仍需核对实际 Git 状态。

仓库远端为 `Lee-NG915/dtc-support-agent`。这是项目身份信息，不是合并、部署、访问真实店铺或发送消费者消息的授权。
