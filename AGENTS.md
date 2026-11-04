# DTC Support Agent 工作约定

本仓库的目标与当前实现状态以 [项目方案](docs/project-plan.md) 为准。首期服务企业内部客服和审批人：查询订单与政策、生成草稿、人工审批、创建工单并核对结果。消费者诉求是输入；直接面向消费者自动回复、真实退款和自动取消订单不属于首期范围。

## 从这里进入

1. 先核对当前任务、工作区、分支与既有授权；读取 [项目档案](.dtc/target-profile.md) 和受影响文档。
2. 开发助手技能统一放在 `.agents/skills/`；`app/skills/` 是应用运行时的业务配置，不在此安装开发技能。
3. 按任务选择下表技能，只读取当前阶段需要的参考。创建或实质修改文档时同时使用 `dtc-documentation`。

| 任务 | 技能入口 |
|---|---|
| 接手或恢复任务、核对范围 | [dtc-context](.agents/skills/dtc-context/SKILL.md) |
| 梳理需求与验收 | [dtc-requirements-analysis](.agents/skills/dtc-requirements-analysis/SKILL.md) |
| 比较方案、接口与可靠性设计 | [dtc-technical-design](.agents/skills/dtc-technical-design/SKILL.md) |
| 实现服务或前端功能 | [dtc-implementation](.agents/skills/dtc-implementation/SKILL.md) |
| 测试、模型评测、审查 | [dtc-scoped-validation](.agents/skills/dtc-scoped-validation/SKILL.md) |
| 调试异常与查证原因 | [dtc-troubleshooting](.agents/skills/dtc-troubleshooting/SKILL.md) |
| 更新决定与验证记录 | [dtc-evidence-log](.agents/skills/dtc-evidence-log/SKILL.md) |
| 提交与开发分支交付 | [dtc-delivery](.agents/skills/dtc-delivery/SKILL.md) |
| 文档设计、源码讲解与学习 | [dtc-documentation](.agents/skills/dtc-documentation/SKILL.md) |

## 文档与学习

[文档规范](docs/guides/documentation.md) 是文档设计的人读正本，从 Hextech 的文档归属、稳定主题和证据原则适配而来。以 Markdown 为主，必要图表同时有文字说明。

使用者有前端经验，正在学习 Python、SQL、Agent 与部署。先解释业务问题，再解释术语、真实源码、设计取舍、失败情况和可验证的小实验。不要堆框架名字，也不要把计划接口写成现有功能。入口是 [学习路线](docs/learning/README.md)。

方案、操作指南、学习说明、验证记录各有职责；同一主题持续维护。未经实际运行，不能写通过率、上线、用户采用或效率提升。上游历史数据保留来源边界，不作为本 fork 成绩。

## 实现与验证

- 沿用实际 Python/React 目录和配置，不套用 Nx、店铺或私有平台命令。依赖事实来自 `pyproject.toml`、`uv.lock`、`frontend/package.json` 和 CI。
- 默认使用合成数据与 Fake；外部写入、真实数据和付费模型遵循当前授权。模型输出和检索资料不授予操作权限。
- 本地测试、真实模型评测、远端文件回读、真实试用分别报告。改配置和依赖后补相应检查，文档变更不强制启动整个应用。
- 新技能必须自包含，引用存在、描述准确、无私有凭据或机器绝对路径。来源与适配说明维护在 [技能指南](docs/guides/skills.md) 和 `.agents/skills/sources.json`。
- 文档/技能检查：`python3 scripts/check_project_docs.py`；提交前检查 `git diff --check`。自动检查只验证结构，不代替人工核对内容与代码。

## 协作与交付

复用或建立带负责人、范围和验收的 GitHub Issue。开发分支通常为 `codex/<issue-number>-<topic>`，不能跟踪远端主分支。只暂存当前任务文件，保留无关改动。

提交、推送、合并和部署按用户实际授权执行；已有授权不重复请求，技能也不扩大授权。进度追加到对应 Issue，保持文档、分支和提交可追溯。当前规划任务见 [Issue #1](https://github.com/Lee-NG915/dtc-support-agent/issues/1)。

持续同步约定（2026-11-04 用户确认）：

- 每次功能进展、方案或约定变化时，同批更新受影响文档与对应 Issue 评论，保持代码、文档、Issue 一致；文档状态按证据标注（计划 / 已确认 / 已实现 / 已验证 / 已推送）。
- 拆分出的子任务使用 GitHub sub-issue（创建时指定父 Issue），不用独立无关联 Issue 代替；跨仓库任务在各仓建本仓 Issue 并互链，整体验收由产品中台 Issue 跟踪。

跨仓库售后契约（2026-11-04 用户确认）：

- [产品蓝图](docs/architecture/modules/after-sales/product-blueprint.md)是产品行为唯一正本；`contracts/epig-after-sales/v1/openapi.json` 是线上字段、路径和状态码唯一正本；[对接规范](docs/architecture/modules/after-sales/epig-contract.md)负责可靠性语义与变更流程。禁止以对话、Issue 或任一仓库的 DTO 覆盖这些正本。
- 契约变化必须在同批更新产品语义、OpenAPI、共享 fixtures、检查与验证记录，并同步 DTC#2 和 E-pig#2；任一方发现缺口先提出变更，不得单方面修改线上字段、枚举或状态语义。
- E-pig 只能使用带不可变源提交和逐文件 SHA-256 的 vendor 契约包，不得手改副本。实现完成必须提供源版本、digest、共享正反 fixtures 和本仓验证结果。
- 开发窗口开始线上 DTO、Connector 或 Route Handler 工作前，必须回读对应 Issue 的最新契约交接评论；本次源契约存在不代表 E-pig vendor、跨仓 freshness 或运行时联调已完成。
