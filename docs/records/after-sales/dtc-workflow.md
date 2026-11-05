# DTC 内部业务流程实施与验证记录

更新日期：2026-11-06。当前恢复点：DTC-2a员工身份/Connector已验收推送d3cab38，关键文件/远端SHA回读一致；DTC-2b已验收推送8d68686，Worker核心已验收推送b01513b，远端SHA/关键文件字节回读一致，跨仓HTTP仍独立验证，E-pig5d已完成本地/CI验收并推送，消费者组件已实现但实际浏览器验收受工具阻塞。分工以[协作正本](../../guides/ai-collaboration.md)为准，方案见[业务流程设计](../../architecture/modules/after-sales/dtc-workflow.md)。原始临时日志不承诺长期保留，本文保存可重新执行的命令和精简结果。

## 2a首轮与独立核验

候选基线`edc8443831ad23c4ab6675f9dc1c998221cbd39b`加未提交源码。Claude首轮实现达到120turn上限，无完整最终报告，不据此判断通过。Codex实际执行：

| 检查 | 结果与边界 |
|---|---|
| 文档结构与diff空白 | 当时32 managed文档、9技能通过，diff --check通过；只证明结构 |
| Connector三文件聚焦pytest | 62 passed，使用MockTransport/Fake，不代表跨仓真实HTTP |
| 身份/API/迁移聚焦pytest | 13 passed、1 skipped；PG缺显式环境，不能算PG通过 |
| 当时全量pytest | 319 passed、2 failed、1 skipped；SSE全局Event跨event loop，以及cleaning日志capture失败，待最小修复与复跑 |
| 专用真实PG迁移独立运行 | `pytest -q tests/integration/test_after_sales_migrations.py`，4 passed、exit0；新建合成PG16容器，环境仅传本次临时连接，finally删除本容器及匿名卷，container_removed=true |

PG日志在本机临时`/tmp/dtc-2a-codex-pg.log`，不包含凭据，容器已销毁。此证据只证明迁移路径，不证明后续审批/Worker并发。

独立只读Review发现响应业务身份/receipt关联核验缺失，以及开发环境validation错误保留input。基础feature关闭、逐请求员工认证、角色/市场、session归属、显式Settings生命周期及PG启动不create_all已看到实现；看到代码不等于所有运行场景通过。Codex已向Claude auto下发聚焦返工，要求保留流式权限覆盖和失败历史。

## 2b/2c设计Review

方案Review发现prepared操作因批准失效后无法释放活跃限制，以及旧feed页可能覆盖新结果。已分别补产品7.4的prepared→failed（仅确认从未派遣）与同步防倒退约束。它们是内部流程守卫，不改变线上OpenAPI或vendor。实现尚未开始，不能把方案Review当功能验收。


首轮返工报告（Claude，非最终验收）：身份/响应关联/错误脱敏修复后全量353 passed、1 skipped，Ruff格式/静态及mypy135源文件通过；流式接口在建立200前校验身份/市场/session，测试间隔离SSE全局Event，Alembic日志配置保留已有logger。独立Review随后发现“已发送POST后的协议错误分类仍不是未知写入”，已要求统一LOOKUP_FIRST并补回归；上述353项不是最终候选结果。


## DTC-2a最终本地验收（2026-11-06，Codex）

结论：本章范围通过；已提交推送`d3cab389429ab27a2f0d5483fb6388afc9e05bed`，远端SHA及关键源码/文档逐字节回读一致。最终候选已补所有成功对象/回执身份核验、固定字段错误脱敏、SSE开始前权限/session检查、测试Event隔离及Alembic日志回归；已发送建单的协议异常统一LOOKUP_FIRST/outcome_unknown且不自动重发。只读和本地输入错误保持原分类。

Codex在Claude任务结束后独立执行：Ruff format/check exit0（194文件）、mypy135源文件无问题、全量pytest **364 passed、1 skipped、1 warning**（6.41s）；skip为默认不提供专用PG URL的测试，独立PG实测4通过的证据见上节，未把skip改写成通过。文档结构33 managed/9技能与git diff --check通过。全量日志在本机临时`/tmp/dtc-2a-codex-final-pytest.log`。

剃刀结论：复用原API/容器、SQLAlchemy和httpx；增加一个staff表与一个业务Connector接口；不新增鉴权平台、通用工作流或业务写API。必要权限/错误/迁移/协议恢复测试保留。没有真实跨仓HTTP、持久审批、Worker、员工UI、真实模型评测或生产试用证据，这些继续按后续章节推进。未改线上契约，后续E-pig源锁仅需显式元数据更新。


## DTC-2b实施开始

基线d3cab38，Codex已下发已Review方案给Claude auto，范围为持久同步/草稿/材料/审批与prepared操作；批准不直接调用外部建单，Worker下一片段实施。未完成前不计入验收，相关源码/迁移/测试以实际返回结果核验。


2b首轮与Review（2026-11-06）：Claude首轮到110turn上限，中间全量385 passed、1 skipped及静态门禁通过，未形成最终交付。Review确认仅凭远端ticket_created完成、队列自由文本PII、必要动作材料门槛不足；已交Claude auto返工。序号存储、政策日期与SQLite时间比较在候选中已修正，待固定候选复验，不能把中间通过数当最终验收。


## DTC-2b最终本地范围验收（2026-11-06，Codex）

结论：本章范围通过；已提交推送`8d68686`，远端SHA与关键源码/产品语义/记录逐字节回读一致。交付持久case/游标、内部材料、可信SKU/政策日期证据、不可变草稿、人工审批、稳定prepared操作与事件；禁止自批、越市场、旧revision和绕过资料/审批。批准尚不创建E-pig工单，Worker下一章实施。

独立Review关闭：同步旧页防倒退、字符串高位sequence、无回执不completed/保留未知上下文、结构化队列摘要、政策日期、动作主题/签收材料、游标SQL原子CAS、未知执行时可保存候选但不覆盖原执行指针。产品7.1澄清可信服务观察已有工单可进入对账，不能伪造本地批准/直达完成；线上契约无变化。

Codex独立最终命令：Ruff format/check通过（203文件），mypy142源文件通过；全量pytest **413 passed、1 skipped、1 warning**（15.80s），默认skip仍为未配置专用迁移URL，不改写为通过；文档33/9与diff --check通过。全量临时日志`/tmp/dtc-2b-codex-final-pytest.log`。

真实PG独立验收：新建PG16专用合成容器，以临时pytest适配器将现有完整workflow测试的Settings数据库切为本次PG、每项仅重建本次合成库public并显式迁移0001～0004，测试断言/生产实现不变。首次48过1失败暴露测试直接asyncio.run复用应用池跨loop，已最小改用TestClient portal；未关闭应用pool或删除并发断言。最终同层 **49 passed、1 warning、exit0**（19.48s），含实际PG并发revision/空页同步CAS、审批/唯一活跃操作/回滚、迁移与权限。容器及匿名卷最终删除，dedicated_container_removed=true。

PG临时日志`/tmp/dtc-2b-codex-pg-workflow.log`保留失败，`/tmp/dtc-2b-codex-pg-workflow-final.log`为最终结果；编排与适配器为本机临时验收产物，分别`/tmp/dtc_2b_pg_review.py`、`/tmp/dtc_pg_acceptance_plugin.py`，不承诺长期保留。后续Worker章补仓内可重跑专用入口，避免恢复依赖这些临时路径。当前PG证据不是跨仓HTTP或多进程Worker恢复验收。

剃刀：复用staff/Connector/SQLAlchemy与现有检索，只有业务生命周期所需的表/状态和明确动作材料守卫，无通用规则DSL/审批平台/新消息队列。工单回执、恢复、AI建议与页面仍未实现/验收，不以本章测试数宣称企业生产可用或业务收益。


2c实施前Review（2026-11-06）：独立复核未见阻断或新增框架必要，补明market/order_id来自原始不可变审批快照而非当前case；原wire不新增字段。下一任务在同一模块新增独立execution与进程入口，复用2b校验，交付可重跑专用PG入口，仍无生产/真实写入授权。


离线政策回归补查（2026-11-06，Codex）：直接运行`.venv/bin/python -m eval.runner --task epig-policy` exit0，34/34，scope/source/review违规均0，model_generation_tested=false；后端为BM25+HashVector+RRF+Lexical，不能当作真实LLM或完整图成绩。可选unstructured缺失使用既有fallback，警告保留。本次未运行旧generation/e2e或任何真实收费模型。


## 2c核心候选与证据纠正（2026-11-06）

Claude首轮达115turn上限；返工候选自测445 passed、1 skipped，含operator IN_FLIGHT保持人工边界，尚未最终验收。Review要求历史unknown不能被后来拒绝或上下文失效抹掉、暂404需原键有界恢复、派遣前fresh授权/版本/条件更新、原审批快照核验与原子消费；对应代码已修复，后台feed自动轮询仍需补，不能由员工手工Sync替代。

作者曾报告临时PG插件执行31项通过；Codex不采纳为PG证据。只读collect-only Probe确认pytest实际收集模块为`test_after_sales_execution`、Settings函数仍`_settings`，而插件修改`tests.integration.test_after_sales_execution`，没有作用于实际收集模块；其reset还只有PG跳过的init_schema，缺显式迁移。这批测试没有证明execution使用PG。既有2b根独立49项使用request.module适配且实际出现/修复asyncpg跨loop错误，证据有效，不回溯抹除。

下一片段将临时适配收敛为仓内专用runner：必须对pytest实际module改Settings、显式迁移，核验fixture engine dialect/current_database/current_user/pg_backend_pid，并做独立进程领取/过期/迟到提交。默认skip与Fake结果不能替代该证据。

后台轮询与仓内PG入口候选（2026-11-06）：Worker已补按配置市场逐页拉取feed，单市场失败不推进游标、不阻断其他市场及已有批准操作；service事件不冒充员工。Claude返回普通测试468 passed、4 skipped及专用入口94 passed。Codex独立普通测试复跑同为468 passed、4 skipped、1 warning，Ruff格式/静态与mypy147通过；专用PG独立复跑仍在进行，不能提前采纳作者结果。专用计数须拆分实际PG与SQLite迁移。

只读Review已确认request.module适配、显式迁移、实际engine身份探针和生产方法的多进程竞争/旧epoch守卫；发现测试子worker另建session，导致runner取消时不能随pytest进程组结束，交Claude作最小修正并补真实后代退出回归。该候选暂不验收推送；跨仓HTTP、实际浏览器及后续章节仍单独验收。

Codex独立专用入口随后exit0：94 passed、1 warning（53.86s），拆分为86个PG适配、3个PG多进程、1个PG迁移与4个SQLite迁移；真实PG为90项。87个live探针均为本次目标且backend PID互异，三组进程竞争分别验证单owner、epoch 1→2隔离旧提交、被杀进程租约持久重领；0001～0005迁移到head，container_removed=true。日志`/tmp/dtc-2c-codex-pg.log`。这是修复清理缺口之前的候选成绩，不能替代修复后的退出回归。

剃刀Review进一步确认手写PG wire/SCRAM仅为保持stdlib，而入口本就依赖已安装pytest/asyncpg的项目环境，缺少实际收益。决定复用现有asyncpg执行显式目标TCP查询，删除自研认证协议；保留连接/查询/关闭统一超时、目标隔离、进程清理和安全错误输出，待Claude实现后验证。

## DTC-2c执行核心最终本地范围验收（2026-11-06，Codex）

结论：本章核心范围通过，当前为728833b基线加本批未提交候选，尚未推送。交付独立Worker/迁移0005、有界feed轮询、租约/epoch领取和迟到隔离、派遣前fresh校验、历史未知保护、原key/payload有限恢复、原批准回执核验与消费，以及无PII的operator查询；无新增工作流平台/消息队列。跨仓真实HTTP、消费者与员工界面、完整故事、Compose及真实模型不计入本次核心验收，M3/M4尚未整体完成。

最终候选独立结果：Ruff格式215文件、静态检查及mypy147通过；普通pytest **476 passed、4 skipped、1 warning**（32.10s）。skip为3个专用PG多进程与1个需专用URL的迁移，不能算PG成绩。专用入口再次独立exit0，**94 passed**：90实际PG（86适配、3多进程、1PG迁移）与4 SQLite迁移；PG16.15、0001～0005到head、87实际engine身份探针且backend PID互异，三组真实OS/PG进程验证单owner、过期epoch隔离与崩溃重领，container_removed=true。日志`/tmp/dtc-2c-codex-final-pytest.log`、`/tmp/dtc-2c-codex-final-pg.log`；源码候选21文件SHA-256清单`/tmp/dtc-2c-final-candidate-sha256.json`，验证期间无变化。临时产物不承诺长期保留，仓内命令可重跑。

Review关闭：实际pytest模块适配和数据库身份、旧unknown不可误释放、新候选不能覆盖原操作、manual IN_FLIGHT不重开自动执行、service后台轮询、测试后代进程退出与原进程组覆盖。根独立修复后进程清理unit22项先通过；最终普通全套含asyncpg简化后的26项runner回归。手写wire/SCRAM已删除，复用固定asyncpg连接且共享探测预算，无新依赖。源码讲解/专用命令随本批更新；下一步提交推送回读后继续真实跨仓旅程与员工工作台。


2c交付回读（2026-11-06）：源码/测试/对应文档已提交推送`b01513bb5ed79d52f08fc046905fa287eb55767c`至`codex/2-epig-after-sales`。根核对远端ref与本地HEAD一致，并逐字节回读execution、PG runner、流程设计与实施记录一致。实际Actions查询该SHA为0个run，未宣称远端CI通过；未合并/部署。后续员工后端实现已恢复，跨仓真实HTTP与浏览器边界仍保持独立。
