# E-pig 售后契约 v1：验证与审查记录

更新日期：2026-11-05。状态：本地契约检查与 Codex CLI Review 已完成，审查发现的隐私、空白输入、状态一致性和序号回滚问题已在候选契约/规范层修复；应用实现、E-pig vendor 和真实跨仓联调尚未完成。

## 范围

本记录保存 [产品蓝图](../../architecture/modules/after-sales/product-blueprint.md)、[对接规范](../../architecture/modules/after-sales/epig-contract.md) 和 `contracts/epig-after-sales/v1/` 的实际检查、独立审查与交付证据。结构检查和共享样例不能证明 API、Connector、页面、数据库事务或故障恢复已经实现。

候选契约包含 **13 个操作、49 个 manifest 样例**，以及一个 68 字节的原始 PNG 附件样例。包版本为 `1.0.0`，线上兼容主版本为 `1`。

## 本地验证

### 2026-11-04 初始候选基线（历史）

本机没有预装 `uv` 命令，验证时使用临时 uv 0.13.7 执行；下表用标准命令表示相同操作，不依赖某个本机绝对路径。

| 检查 | 实际结果 | 证据与未测范围 |
|---|---|---|
| `uv sync --frozen --extra dev` | 通过 | 锁文件环境可安装；只重装本项目包，未访问真实 E-pig |
| `uv run python scripts/check_epig_after_sales_contract.py --write-checksums` | 通过 | OpenAPI、43 个样例、需求/验收 ID、全部响应版本头、路由鉴权、写约束、二进制 digest 均通过；显式生成 `SHA256SUMS` |
| `uv run pytest -q tests/contract/test_epig_after_sales_contract.py` | **13 passed** | 包含缺字段、游离样例、错误需求 ID、版本不一致、虚构 Cookie、Schema 指针漂移、错误响应缺版本头、响应头非必填、消费者额外字段、二进制 digest/大小和过期 checksum 等负例 |
| `uv run python scripts/check_project_docs.py` | **PASS：26 个 Markdown、9 个开发技能** | 只检查结构和本地链接，不证明产品语义或外部链接 |
| `uv run ruff format --check .` / `uv run ruff check .` | 通过 | 164 个文件格式一致，lint 通过 |
| `uv run pytest -q --cov=app --cov=eval --cov-report=term:skip-covered --cov-fail-under=70` | **176 passed，1 warning；覆盖率 71.21%** | 包含 13 个契约测试；LangGraph 有一个 pending deprecation warning，未运行真实模型或跨仓联调 |
| `uv lock --check` / `git diff --check` | 通过 | 锁文件一致，补丁无空白错误 |
| `uv run mypy` | **未通过：1 个既有错误** | `app/providers/deepseek.py:53` 对 `object` 调用 `int()`；该文件不在本次 diff 中，本任务未修改应用代码。因此不能宣称完整 CI 已通过 |

远端 CI 尚未针对本次候选运行；以上均为本地结果。开发分支普通 push 不触发现有工作流，只有 main push、以 main 为目标的 PR 或手动 dispatch 才运行。

### 2026-11-05 Codex Review 后最终检查

| 检查 | 实际结果 |
|---|---|
| `.venv/bin/python scripts/check_epig_after_sales_contract.py` | 通过：OpenAPI、**49 个 fixtures**、ID、请求头、写入约束、安全 DTO、状态联动和二进制 digest |
| `.venv/bin/python scripts/check_epig_after_sales_contract.py --write-checksums` | 语义检查通过后显式重建 `SHA256SUMS`；随后不带写参数复验通过 |
| `.venv/bin/python -m pytest tests/contract/test_epig_after_sales_contract.py` | **13 passed** |
| `.venv/bin/python -m pytest --cov=app --cov-report=term-missing` | **242 passed，1 warning，总覆盖率 80%** |
| `.venv/bin/python -m mypy` | **125 个源文件通过**；配置范围为 `app`、`eval`，provider 类型错误已修复 |
| Ruff format/check、文档结构检查、两仓 `git diff --check` | 通过 |

上述结果仍是本地候选验证，不是 E-pig 运行时、数据库事务、真实附件或跨仓联调验收；远端 CI 未运行。

## 此前独立 Review

### 第一轮：发现契约阻断项

独立审查者读取完整产品 diff、OpenAPI、fixtures、检查器与真实测试输出，提出 10 项阻断问题：

1. 不同幂等键可能给同一诉求建多张工单。
2. `order_version` 仅作审计，不能阻止订单已变化的写入。
3. 序号分配与提交可见顺序、非空末页游标和游标过期存在漏数风险。
4. 回执查询不能区分处理中和不存在，写错误重试语义不清。
5. 消费者响应允许额外字段，可能泄露审批字段、地址或存储 URL。
6. Cookie 主体与订单归属、附件上传绑定和 service 下载鉴权不完整。
7. 空描述的线路校验与 `needs_info` 产品旅程冲突。
8. “原始表示”重放可能用旧状态覆盖新状态。
9. 检查器信任独立 Schema 指针，且二进制和错误响应版本头覆盖不足。
10. 精确 `1.0.0` 请求头与兼容 minor 升级冲突。

修正包括：创建事务与 `(market, request_id)` 原子唯一约束、当前订单版本守卫、连续已提交高水位、非空游标、`202 IN_FLIGHT` 和确定性恢复动作、消费者生产白名单、稳定 `cart_id` 归属、上传订单绑定、分离 service 附件路由、入口必填校验与 `needs_info` 分工、原对象当前表示重放、操作反推 Schema、原始二进制校验，以及包 SemVer 与线上主版本分离。详细规则以产品蓝图、对接规范和 OpenAPI 为准，本记录不复制字段定义。

### 第二轮与最终复查

- 原独立审查者逐项复查后确认：**10 项问题在契约层面全部解决，没有剩余阻断性契约缺陷**。同键 `404` 恢复不再宣称权威不存在，迟到请求由原子幂等和一诉求一工单约束去重。
- 随后补查的响应头必填、有效二进制大小上限和 `processing` 文案也已修正。
- 最终审查者再次确认这三项关闭，并独立重算确认 `SHA256SUMS` 当前有效。
- 当时 `SHA256SUMS` 文件自身 SHA-256：`d273ea15a4caf5cf48b991511a3a9f6db6ad2173ceb3139811caf00c66932925`。后续契约修复已重新生成该文件，此哈希仅为历史证据。

Review 是静态候选审查，不等同于运行验证、用户接受或发布授权，也不保证所有现实环境无故障。

## 2026-11-05 Codex CLI 复审与处理

本机 Codex CLI 0.166.0、read-only sandbox 分别审查 DTC 已提交和未提交改动及 E-pig 计划。主会话复核后采纳以下契约修复：

| 发现 | 当前候选处理 |
|---|---|
| 消费者错误与 `details` 允许任意额外字段，可泄露审批、地址或 token | `Error`、`ErrorResponse` 封闭；新增只允许安全机器字段的 `ErrorDetails`，生产方白名单覆盖错误 Schema，并加入泄露负例 |
| `ServiceOrder.additionalProperties=true` 与脱敏承诺冲突 | 封闭 Schema、加入生产方白名单，并增加顶层 `full_address` 负例 |
| 空白描述/回复合法 | 创建诉求、消费者/服务描述及创建工单、消费者/服务回复统一增加 `pattern: "\\S"`，加入两个空白负例 |
| `status` 与 `ticket_id` 可矛盾 | 消费者和服务诉求均增加 `pending ↔ ticket_id=null`、`ticket_created ↔ OpaqueId` 条件约束，加入两个状态负例 |
| 附件数组和默认值导致幂等指纹分叉 | 明确缺失等同 `[]`，指纹计算前按附件 ID 排序 |
| 序号分配回滚造成连续高水位永久空洞 | 规范改为每市场串行发布计数器，序号、发布记录和业务写入同事务，回滚同时回滚分配；不引入 outbox/tombstone 新架构 |
| feed 永久重放与市场删除策略冲突 | 明确 v1 不裁剪已发布历史；未来删除前必须先有版本化无遗漏重同步契约 |
| 来源哈希证据被理解为内容完整性校验 | 文档明确当前只做来源标识/哈希格式检查，不声称重算外部内容 |

共享反例使 manifest 从 **43 增至 49**；没有为反例另建契约版本或第二套消费者错误模型。`CONSUMER_ALLOWLIST_SCHEMAS` 虽是显式分类清单，但本轮只为真实安全 DTO 扩展它，没有新增 `x-*` 元数据抽象。E-pig 侧重复路由、pending 队列、第二审批权威等过度设计的完整裁决见 [政策 Review 与剃刀清单](epig-policy-v1.md#5-codex-双仓-review-与剃刀裁决)。

## 跨仓交付状态

- DTC 不可变源提交和远端回读证据：提交后记录到 DTC#2 与 E-pig#2，避免在本文件中写入自引用提交哈希。
- E-pig vendor/source-lock、生成物新鲜度、共享 fixture 运行时符合性和 `pnpm verify`：由 E-pig#2 实现，本记录不预先宣称完成。
- 真实 multipart 上传、伪造 magic bytes、超 5 MiB 文件、错误/过期/跨市场 Cookie、同市场跨 `cart_id` 访问、跨订单附件引用及 Cookie 续期重试：必须由 E-pig 运行时测试覆盖。
- 数据库事务、并发唯一约束、连续高水位、迟到请求、审批重启恢复和真实回执故障演练：未执行。
- CODEOWNERS 与 CI 配置已加入候选；GitHub 分支保护是否将代码所有者审查和检查设为必需，尚未核验。
- 原 `deepseek.py` mypy 错误已在本轮修复；配置范围 mypy 通过，不能再把该错误列为当前阻塞。
