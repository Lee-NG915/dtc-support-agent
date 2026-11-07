# 开发技能使用与选择说明

更新日期：2026-11-06。安装范围：当前仓库 `.agents/skills/`，随开发分支提交。没有全局安装，也没有修改来源仓库。

## 为什么这样组合

选择标准是当前业务匹配、读者理解成本、代码可追溯、验证能力和长期维护量，不按文档数量或模板复杂度排名。

| 候选 | 适合保留的部分 | 本项目处理 |
|---|---|---|
| Hextech 的 `docs/README.md` 与 `product-design-tech-spec` | 文档职责、稳定主题、唯一正本；方案对比、接口/状态/失败契约、真实代码依据 | 作为 [文档规范](documentation.md) 和 `dtc-documentation` 的基础；移除店铺、多应用和私有流程约定 |
| DTC-skills 的阶段技能 | 从上下文到交付的可组合流程，适合按需加载 | 适配七个阶段技能；修正目标项目和引用，不安装未用的页面/视觉技能 |
| 本项目上游手册与 [教学蓝图](../18-teaching-doc-blueprint.md) | 概念、源码、原因与练习相互联系，深入讲解检索和 Agent | 保留作为深读资料；新增更短的业务学习入口，从第一个案例就定义验收，系统评测知识仍可后续深入 |
| 本地 skills 的 `design-doc-style` / `architect` | 视觉一致性、决策与边界表达、跨文档一致性 | 比较后不整套安装。前者面向已选定 HTML 的设计文档，后者包含大量平台流程与依赖；当前 Markdown 方案维护量更低 |
| 本地 skills 的 `testing-strategy` | 分层测试、异常恢复、需求对应测试、防止测试未覆盖路径却通过 | 改写进验证技能的 [测试设计参考](../../.agents/skills/dtc-scoped-validation/references/test-design.md)，不引入私有 CI、固定比例或 HTML 强制要求 |
| 本地 skills 的 `root-cause-analysis` | 比较可能原因，用真实证据判断，区分恢复与暂时不报错 | 改写为 `dtc-troubleshooting`，使用本项目请求链和工单例子 |
| 本地 skills 的 `local-fullstack-debug` / `agent-capability-delivery` | 特定环境联调、受管交付流程 | 当前不安装：分别面向 Android/特定后端和指定平台仓库，不能作为这里的通用命令 |

结果是 **9 个自包含开发技能 + 一份人读文档规范 + 一条学习路线**。这套选择适合当前规模；若以后确有图形文档站、真实客服平台或生产运维需求，再比较相关专用技能。安装技能本身不代表获得平台权限或项目能力已经实现。

## 技能如何调用

先在助手中打开本仓库。支持项目技能发现的助手可在新一轮会话加载 `.agents/skills/`；如列表暂未刷新，新建会话或重新打开项目。也可明确要求按 [AGENTS.md](../../AGENTS.md) 读取对应 `SKILL.md`，无需复制到全局目录。

| 技能 | 使用时机 | 示例 |
|---|---|---|
| `dtc-context` | 接手或恢复工作 | `$dtc-context 核对当前范围与下一步` |
| `dtc-requirements-analysis` | 业务需求不清楚 | `$dtc-requirements-analysis 梳理工单草稿的验收` |
| `dtc-technical-design` | 需要选择实现方法 | `$dtc-technical-design 比较审批状态的保存方式` |
| `dtc-implementation` | 已有明确功能目标 | `$dtc-implementation 实现当前确认的最小接口` |
| `dtc-scoped-validation` | 测试与评测 | `$dtc-scoped-validation 设计重复请求和失败恢复验证` |
| `dtc-troubleshooting` | 异常或不确定原因 | `$dtc-troubleshooting 查明请求卡在哪一层` |
| `dtc-evidence-log` | 完成阶段或准备续接 | `$dtc-evidence-log 更新当前主题的结果和未完成项` |
| `dtc-delivery` | 准备分支交付 | `$dtc-delivery 检查本次改动与验证，按当前授权交付` |
| `dtc-documentation` | 设计文档或学习讲解 | `$dtc-documentation 用客服例子解释实现，并给小实验` |

不需要每次调用全部技能。比如修改一个文档，只需上下文、文档规范和文档检查；实现审批功能才按需求、设计、实现、验证推进。技能不自动推送、部署、发消息或运行收费评测。

技能是各阶段的检查清单，不改变 [AI 协作分工](ai-collaboration.md)：当前 Codex 负责文档、需求与方案、开发/测试计划、Review、验收验证和最终验收；Claude 负责实现和必要的单元测试，Claude 自测不能替代 Codex 的最终验收。

## 来源与更新

- 七个阶段技能来自 [DTC-skills](https://github.com/Lee-NG915/DTC-skills) 的本地版本，已按 Python/Agent 项目改写。保留 [MIT 声明](../../.agents/skills/LICENSE-DTC-skills)。
- 文档规范参考本地 Hextech 的通用原则；排障与测试参考本地 skills 的通用方法，以本项目语言重新编写。未携带私有服务地址、品牌资料、凭据、部署脚本或公司审批规则。
- [来源清单](../../.agents/skills/sources.json) 记录所检查的提交、相对源路径、内容哈希与适配说明。哈希用于辨别来源版本，不表示本项目文件与来源逐字相同，也不表示来源已对外发布。
- `.agents/skills/` 是本项目的适配副本，不用软链接指向维护者电脑，也不自动跟随外部更新。`app/skills/` 继续仅负责应用业务配置。

更新时先比较来源差异，再按项目约束选取，维护适配说明；不要直接覆盖已经补好的业务边界。新增/删除技能同时更新 AGENTS 路由、来源清单和本指南，再运行检查器。不要在没有重新核对内容的情况下只修改源提交号。

## 维护检查

```bash
python3 scripts/check_project_docs.py
git diff --check
```

检查名称、清单与目录一致、元数据、必要引用和本地 Markdown 链接。外部链接、图的呈现、命令语义和真实业务行为需要另行核对。技能包符合结构也不保证助手每次自动选对，需要在实际开发任务中观察是否按流程执行。
