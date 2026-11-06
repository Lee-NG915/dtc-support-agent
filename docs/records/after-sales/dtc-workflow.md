# DTC 内部业务流程实施与验证记录

更新日期：2026-11-06。当前恢复点：DTC-2a员工身份/Connector已验收推送d3cab38，关键文件/远端SHA回读一致；DTC-2b已验收推送8d68686，Worker核心已验收推送b01513b，远端SHA/关键文件字节回读一致，真实HTTP恢复片段已通过，员工证据/建议后端已验收推送d9e97ff，远端SHA/关键文件回读一致，完整旅程仍待验收，E-pig5d已完成本地/CI验收并推送，消费者组件已推送72f1621，服务层八场景与备份恢复已独立验证；两仓Compose空feed启动已独立验证，独立Chromium页面与有效真实模型保留集仍待最终验收。分工以[协作正本](../../guides/ai-collaboration.md)为准，方案见[业务流程设计](../../architecture/modules/after-sales/dtc-workflow.md)。原始临时日志不承诺长期保留，本文保存可重新执行的命令和精简结果。

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


## 员工后端首轮与Review（2026-11-06，尚未验收）

在aa2be9e后，Claude已返回case evidence/ticket只读、严格结构化建议、冻结审批snapshot白名单及共享ticket校验候选。Codex独立Ruff格式217文件/静态、mypy148通过，全量普通497 passed、4 skipped、1 warning（33.86s）；没有真实模型质量或PG增量结论。

独立Review实际复现P1：模型返回后的第二次源订单读取回调把support撤为approver，case revision未变，接口仍200且保存1草稿。原因是fresh staff检查在远端await之前，真实保存事务未再查权限；已交Claude将最后授权重读放在保存事务内，403且不得新草稿/event或失效旧prepared。另fresh证据沿用生成前as_of，跨UTC日可能接受旧日条款，要求当前日期核验和409边界回归。现有严格字段/非空真实引用/Offline明确不可用/冻结快照/原批准工单身份已通过只读核对，仍不能用497项代替修复后的验收。

跨仓临时旅程作者曾返回197c9de8e6a84bc0通过：真实上游提交后阻断响应、杀Worker、自然租约过期、新进程按原键回执恢复、外部单例及消费者归属；尚未采纳最终验收。Review要求临时脚本只清精确所属路径、验证整个进程组退出，并对固定b01513b源码运行，避免并行员工候选影响证据；第一次TypeError失败保留。根已准备无.env的干净b01513b归档；其结构检查32篇/9技能通过，和共享工作区33篇的区别来自未提交学习文档，不把后者假作远端文件。

## 2c真实跨仓HTTP恢复验收（2026-11-06，Codex）

结论：此项通过；不是消费者/员工界面、AI质量或六故事整体验收。根独立运行`PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python /tmp/dtc_epig_e2e/run_journey.py`，exit0，报告`/tmp/dtc_epig_e2e/runs/9b9ad702f882499d/report.json`；203个旅程检查点不是203项独立业务测试。DTC API/Worker/知识/迁移来自无.env的固定b01513bb5ed79d52f08fc046905fa287eb55767c归档，依赖复用项目venv，未混入并行员工候选；E-pig使用新专用持久运行时，凭据仅本次合成。临时编排/报告不承诺长期保留。

实际经HTTP提交诉求、同步、人工材料、新草稿、不同员工送审/批准。真实E-pig建单201后，回环代理尚未转回响应时杀Worker1（PID33288）；本地dispatching、attempt1、有租约且receipt_json为空，源requests/tickets/keys已1/1/1。等待自然租约到期8.96s，Worker2（33307）先按原key查再GET核验，claimed1/succeeded1；原key/payload摘要保持，case completed、operation succeeded/reconciliation matched、原approval/draft consumed。Worker3（33355）claimed0，代理累计建单POST仍1，DTC operation/approval/draft仍1/1/1，E-pig仍1/1/1。消费者真实GET诉求/工单200并读到最终回复；第二主体读本单两接口404，错误cookie401。

Review先后修复临时脚本的glob/mtime差集清理、仅组长退出检查；作者历史a2ab失败、197c9通过、b2bba清理失败及777283复跑保留，不改写。根另行只读ps核对所有记录PGID的成员列表为空；Docker daemon健康27.3.1下精确查两个容器和两个所属匿名卷均不存在，精确owned copy/data目录也不存在。没有全局清理或现有业务库。

脚本后续EPERM被误当组消失的通用分支另作局部修复：只有ps确认无成员/全zombie才判无live成员，探测失败/非zombie失败关闭；本次根真实清理证明不依赖旧分支。未为命名额外增加同义对账事件：operation持久matched/lookup_count/unknown_attempts及operation_succeeded、case_completed事件保留恢复事实。feed负责新增诉求，不要求票据状态变化重新发布一条；本次恢复由原操作key查询完成，不伪造remote_ticket_unverified事件。


## 员工证据/工单/建议后端最终本地范围验收（2026-11-06，Codex）

结论：本章后端通过，aa2be9e基线加本批候选尚未推送。交付三个内部case子入口、真实scope证据、只读ticket原批准核验、冻结审批snapshot白名单与严格结构化候选；复用既有Provider/检索/认证/SQLAlchemy和一个共享只读核验模块，无新表/依赖/模型代理或工作流框架。生成、远端查询在事务外，真实保存事务最后重读授权/UTC日期并CAS，建议只到editing。无模型返回503可人工处理，不用Fake冒充质量。

Codex独立最终：Ruff格式217文件/静态与mypy148通过；普通pytest **502 passed、4 skipped、1 warning**（36.86s）。仓内PG入口已最小纳入26条workbench测试，根独立exit0，**120 passed、1 warning**（56.55s）：116真实PG（112适配、3多进程、1PG迁移）与4 SQLite迁移；PG16.15/0005、113 live engine探针且backend PID互异，三组进程竞争仍exactly_one，container_removed=true。默认skip未算通过。日志`/tmp/dtc-workbench-codex-final-pytest.log`、`/tmp/dtc-workbench-codex-final-pg.log`；10文件SHA-256候选清单`/tmp/dtc-workbench-final-candidate-sha256.json`，验证期间无变化。

Review关闭撤权窗口与跨日证据：五项回归确实到达目标窗口，旧代码全部RED，修复GREEN；含源查询期间role/market撤销、跨日条款失效/仍有效、fresh重验后保存入口跨日，403/409且无新draft/event、旧approved/prepared保持。原始497项通过未被误当最终验收。固定源码的真实跨仓恢复与临时EPERM局部5项根复验见上节；它们不代表新增AI端点真实模型质量或页面通过。下一步提交推送回读后实施员工工作台UI，持续保留消费者浏览器阻塞及评测/Compose边界。


员工后端交付回读（2026-11-06）：已提交推送`d9e97ff806efa88a32be6349a1ab78048809349d`至`codex/2-epig-after-sales`；远端ref与HEAD一致，service/verification/26项workbench测试/本记录逐字节回读一致。查询该SHA实际Actions仍0个run，不宣称远端CI成功；未合并/部署。下一章Claude auto仅在frontend实施员工工作台；独立eval片段仅修默认离线/新临时库/真实模型显式开关，预算未确认不运行真实路径，两个实现范围不互改。


## 员工工作台工程交付（2026-11-06，Codex有条件通过）

结论：仅工程实现/Review/本地门禁范围有条件通过，当前尚未推送；真实浏览器、键盘/窄屏、完整角色和跨仓旅程未验收，M3/M4不勾完成。浏览器连接可列标签页，但导航/reload/DOM/CUA均报timeoutMs工具错误；已向用户集中请求恢复配置，未自行修native host或用其他脚本冒充浏览器。其余授权工作继续。

首轮Claude18项组件通过和build通过仍有阻断：真实StaffApp条件Hook使登录崩溃；旧延迟重试借用新api/generation；编辑输入随后台版本静默重基；A保存等待中输入B，A回包误清dirty而可能送审旧A。四项已集中最小返工，真实登录/退出/换身份/延迟重试与编辑回归均有RED→GREEN，独立Review关闭。根独立最终`npm --prefix frontend test` **9文件23通过**，`npm --prefix frontend run build`通过（514.27kB JS chunk超过500kB提示保留，未宣称性能预算满足）。Node依赖仅增jsdom开发项，未增router/状态/UI框架。

另修真实部署入口：API的StaticFiles(html=True)不自动回退/staff，已为存在的index.html显式提供/staff和/staff/，不为任意API做fallback；无dist保持API-only。3条临时dist/TestClient路由测试覆盖shell/assets、未知API404与无token401/功能关闭，已包含于根同候选Python全套验证。日志`/tmp/dtc-staff-ui-codex-final-tests.log`与`...-build.log`；38文件冻结候选清单`/tmp/dtc-ui-eval-final-candidate-sha256.json`覆盖UI/评测两份独立候选，测试期间无源码变化。

界面实现仅说明状态，不虚构建单/履行；operator不取业务PII，AI不可用仍人工编辑，审批读冻结snapshot，真实ticket失败标陈旧。当前作为开发分支候选推送，不合并/部署、不冒称浏览器已通过。

## 当前对话分工与覆盖率门禁复核（2026-11-06）

用户再次确认当前对话由Codex管理、Claude实现；沿用[协作规范](../../guides/ai-collaboration.md)，不采用注入上下文中相反的分工摘要。Claude调用使用`--permission-mode auto`，Codex负责独立Review、验收和交付裁决。

员工工作台工程候选已推送`1b191a35d9aefd5d139da6886fa29261304aa7b9`，远端ref与关键文件回读一致；此前“尚未推送”是交付前状态。真实浏览器与完整业务旅程仍未验收，不改变有条件通过结论。

本批评测安全入口与CI补充仍待最终验收、提交。Codex普通测试535 passed、4 skipped；额外执行CI同款覆盖率命令时，结果为532 passed、3 failed、4 skipped，覆盖率80.89%达到70%门槛，但整条命令失败。失败集中在专用PG脚本的超时、取消、leader退出后三项真实子进程清理测试：孙进程PID未写出，或未触发预期取消。该差异已下发Claude复现，原因尚未确认；不得仅引用覆盖率达标或普通测试通过认定CI门禁通过。原日志保留在`/tmp/dtc-ci-codex-coverage.log`。

覆盖率差异关闭：Claude复现并通过临时诊断定位到pytest-cov自动启动变量被合成leader继承，间接导入jieba生成数据时追踪耗时挤占原有短超时。仅在`test_run_after_sales_pg.py`为这三个合成子进程移除`COV_CORE_`/`COVERAGE_PROCESS_`自动启动变量；业务脚本、超时、真实孙进程、取消及清理断言均保持。Codex审查diff后独立运行该文件：26 passed、1 warning（81.90s）；随后独立执行完整CI同款覆盖率命令：**535 passed、4 skipped、1 warning，覆盖率80.89%，70%门槛通过**（231.14s）。日志分别为`/tmp/dtc-process-coverage-codex-review.log`和`/tmp/dtc-ci-codex-coverage-final.log`。此前失败记录保留；此结果为本地验证，不等于远端Actions或浏览器通过。

评测/CI本批验收裁决（2026-11-06，Codex）：通过本地工程与离线安全范围，待本次提交推送。独立Review无新增阻断；最后测试夹具补充将真实CHILD_POPEN_OPTIONS由父进程写入合成leader输入，避免子进程导入整个应用，原真实进程/超时/取消断言保留。最终文件聚焦覆盖率复验26 passed、1 warning（35.75s，`/tmp/dtc-process-final-options-review.log`）；完整535/4/80.89%验证后仅此夹具范围调整，业务源码未变。Ruff格式220文件/静态、mypy148通过。

专用PG本批首次启动在pytest前因“published port is not bound to loopback”安全失败，所属容器已清理；原port输出未保留，原因未知，未修改或放宽loopback守卫。三次所属临时容器端口探针均正常；带安全端口观测的一次重试为120 passed（116真实PG+4SQLite）、113 live engine探针、0005，容器清理。记录`/tmp/dtc-eval-codex-final-pg.log`、`/tmp/dtc-pg-port-probe.json`、`/tmp/dtc-eval-codex-pg-retry1.log`；不能声称启动偶发现象已定位修复。默认generation/e2e/epig-policy/all在污染父环境和自有假.env下独立exit0、无canary泄漏、临时库清理；epig-policy34/34仍仅离线政策开发集。资源关闭日志敏感正文泄漏有真实RED/GREEN回归，最终日志只保留资源/异常类型。

新增CI仅配置前端Node24和专用PG门禁，未变权限/部署；本地通过不代表远端Actions执行。完整业务工具轨迹、12条保留集、真实模型质量、真实浏览器、Compose及备份恢复仍分别待验收。Claude修复调用达到max-turns退出未提供最终摘要，Codex以实际diff、原始诊断和独立复验裁决，未将CLI退出写成成功。

评测安全/CI交付回读：已推送`296e7af2f9ed97e68e8216ac22dc6997ec916ce7`至`codex/2-epig-after-sales`，远端ref与5个关键文件逐字节一致；该SHA实际Actions查询仍0条。DTC#2评论6673137643、E-pig#2评论6673148337正文已回读一致，两Issue摘要有限更新，保留历史。未合并或生产部署。下一章Docker/Compose基础与eval工具真实轨迹在独立文件范围实施；浏览器页面控制仍失败，真实质量评测的Provider/model/累计预算与专用凭据配置仍待确认，未执行新增付费调用。

## 工具轨迹与部署基础候选验收进行中（2026-11-06）

工具轨迹独立Review关闭重复parse、截断/脱敏碰撞与“全错被标未测”的问题；最终根聚焦37 passed（2.78s）。默认e2e CLI根运行实际记录7次Tool.run（get_order3、track_shipment3、search_knowledge1），仅既有upstream_demo离线集合，未声明期望所以accuracy=null，真实模型质量未测，Connector未混算。报告`eval/reports/e2e_20261106T003600Z.json`、日志`/tmp/dtc-tool-trace-codex-final.log`，不作为完整售后任务成绩。

部署Review阻断的镜像默认值回归已由Claude恢复production与三本地加载false，补真实Settings/管理认证语义回归。作者最终镜像/Compose实测通过，Codex独立固定296e7af源码加3文件叠加后验证：镜像`sha256:8e18123e3d2dc7d3c3a03bad8048a1091b5f83ed88a0224ec69dec412a81cd1d`；health/ready/staff200，员工业务无token401，0005，migrate退出0才启动API/Worker，PG PortBindings={}，API127.0.0.1，Worker once退出0。报告`/tmp/dtc-accept-d9c503aa0b-report.json`含叠加文件digest，所属容器/卷/网络均0、镜像/专用凭据/控制源码已清理。保留首次构建DNS失败`/tmp/dtc-accept-9fe310701d-report.json`；显式拉取相同官方ECR/ghcr基础镜像后第二次已构建并完成健康/权限/迁移，但验收脚本对compose port文本作非空断言失败（`/tmp/dtc-accept-af1918ce7a-report.json`），未当成功；最终改查真实PortBindings后通过。修验收脚本不改产品守卫。此范围仅DTC离线Fake部署基础，普通bridge允许出站，不是两仓联调或备份恢复。

同候选完整覆盖率门禁曾为581 passed、1 failed、4 skipped、81.39%：跨日政策失效测试返回conflicting_policy，未到预期重验阶段；已交Claude定位，暂不最终验收/提交本批。旧失败保留在`/tmp/dtc-delivery-next-coverage.log`。静态Ruff223/mypy149通过。Codex另准备12条保留样例和冻结清单，明确prepared_not_run，当前无真实模型质量成绩；不得把工程守卫混算模型准确率。

跨日失败根因关闭：先_faults_chunk以真实当天读取缓存，再原位修改corpus而未改变revision，fresh日命中旧缓存使严格SourceContext拒绝失效chunk，返回conflicting_policy；日期区间本身有效，未发现生产缺陷。Claude仅修测试：固定11-05生成/11-06重验，先设置生成时钟再首次取证据，明确模型调用一次且hook跨日；保留missing_policy_evidence/409/零draft及event断言。26项工作台与6项相关回归作者通过，独立Review确认实际覆盖生成后重验；当前根完整覆盖率与专用PG最终复验进行中，旧失败不删除。

## 部署基础与工具轨迹最终工程验收（2026-11-06，Codex）

本批工程范围通过。最终根Ruff格式223文件/静态、mypy149通过；完整覆盖率582 passed、4 skipped、1 warning，81.38%，70%阈值不变（104.06s，`/tmp/dtc-delivery-final-coverage.log`）。专用PG最终120 passed（116真实PG+4SQLite）、113个live engine/backend PID、三组真实进程竞争/重领/崩溃恢复、0005、容器清理（60.21s，`/tmp/dtc-delivery-final-pg.log`）。旧581/1失败和首次部署构建/验收脚本失败保留。工具轨迹37项根聚焦及默认CLI实测与部署根实测见上节，不将作者自测当最终验收。

交付范围包含锁定镜像/迁移gate/独立本地Compose、安全默认与构建上下文，实际工具轨迹/精确期望评分边界、跨日测试夹具修复及12条保留样例准备；没有新业务框架/队列/表/依赖。源码运行讲解和操作指南在原主题维护。真实模型/浏览器、两仓Compose完整联调与恢复、性能/实际用户采用仍未最终验收，不宣布完整MVP完成。

## 六故事服务层独立验收（2026-11-06，Codex）

固定DTC源296e7af归档与E-pig消费者dirty候选22文件摘要，Claude作者run aafcf73898c5d424之后，Codex独立run **1022edfdb9fb782a** exit0。8场景均PASS、290场景检查点；另74条setup/共享检查不合并成“测试数”。12次真实Worker进程运行时周期、零模型调用；不是12次CLI参数解析测试，也不是AI质量。报告`/tmp/dtc_delivery_stories/runs/1022edfdb9fb782a/report.json`与summary，根日志`/tmp/dtc-six-story-codex-acceptance.log`。

- 正常：真实PNG/JPEG上传、授权附件读取匹配digest/private no-store，人工草稿v1→v2、另一审批人批准，唯一回执/编辑后的唯一回复。
- 缺证据：缺材料或无政策均409/needs_info，零审批/operation/外部ticket；非法入口400且未创建诉求。
- 越权：第二消费者request/ticket/attachment/代提交404，员工错市场/角色403、缺/坏凭据401，拒绝体无受限内容。
- 陈旧：仅修改所属合成E-pig订单fixture后，旧批准/草稿失效，外部POST0/ticket0，fixture恢复；未SQL伪造批准或完成。
- 丢响应：真实上游201提交后杀Worker组，本地dispatching无receipt；自然租约过期、原key/payload查询并核验，仍只有1POST/1ticket/1key。
- 重启：4次真实API进程组重启，游标/草稿/待审批snapshot/prepared payload保持，继续原批准，重复Worker领取0。
- 内部驳回：仅内部rejected/processing，无operation/ticket；正式reject：经批准建唯一reject票据和不可变最终回复。

独立Review未发现伪造业务状态；发现临时harness cleanup单try异常可能短路其余清理，已下发逐资源聚合错误/finally删秘密的局部修复。该缺口不抹去本次实际清理：Codex额外ps核对21个所属PID/PGID没有任何剩余成员，两个精确容器和DTC卷已不存在，受控copy/data与config.json/staff.json均删除。历史报告保留。后续验证新清理分支及备份恢复；本次仅API/DB故事范围通过，完整AS-ACC仍含真实页面与AI边界，不勾整体完成。


## 数据库恢复独立验收（2026-11-06，Codex）

固定DTC源296e7af与受控E-pig候选的独立run `2285c4f3bdf42db6`完成38项恢复检查，失败0；日志`/tmp/dtc-backup-restore-codex-final.log`，报告`/tmp/dtc_delivery_stories/runs/2285c4f3bdf42db6/backup_restore_report.json`。在E-pig已提交票据、DTC仍dispatching且无回执时停止API，使用pg_dump custom备份；恢复到同一所属PG容器中的另一个全新数据库，E-pig源库不回滚。恢复后迁移0005、原批准/草稿/幂等键/payload保持；先停止Worker，租约自然过期后由操作员调用既有查询入口核对原票据，case完成且原批准消耗，外部POST增量0，之后Worker领取0。

这证明特定跨系统丢响应窗口的恢复与对账，不承诺零数据损失、分布式原子事务或生产恢复时限。备份47959字节、权限0600；目标库实际drop退出0且复查不存在，专用备份/配置清除、所属容器/卷/进程组清理无错误；报告的target_dropped空字段不作为证据。临时harness清理修复另有13项故障注入回归经Codex通过，未将约3000行临时验收工具升级为产品框架。

保留作者首次1302fa0266dc30b0因JSON字符串解析的验收器失败；根首次e6262b5bcbff7551虽38恢复检查通过，但E-pig契约元数据在并行同步中变化，整体按源码完整性失败处理。冻结源码后重跑才得到上述有效结果，不覆盖旧失败。

## 真实模型与页面验收进度（2026-11-06，进行中）

用户明确授权专用DeepSeek真实模型评测、不设累计费用上限，并授权凭据只保存本机受忽略的0600文件；运行仍限制case/call/output。实际3条政策开发集生成通过，只是开发集生成，不代表完整图质量。首次12条保留集执行报告`eval/reports/after_sales_acceptance_20261106T013354Z.json`包含35次模型调用、24443 total tokens；8条模型场景仅observed，4条工程守卫通过，不将observed计为语义通过。独立Review发现H08床垫材料完整描述与实际杯子/空材料夹具不一致，同时复查H07；先修夹具与付费前断言、冻结协议再重测，不修改生产prompt/knowledge或用原报告宣称质量通过。

Codex CLI浏览器插件的页面控制不构成已完成的项目页面验收。当前切换为项目自有独立Chromium软件测试，不控制用户现有Chrome/profile，不要求用户继续安装ChatGPT扩展。页面操作、截图和最终浏览器验收仍待独立复核。


## 两仓Compose空feed独立验收（2026-11-06，Codex）

固定DTC d25dc00与E-pig 72f1621归档，作者构建镜像及受控Dockerfile overlay摘要与根新归档逐一匹配后复用相同image ID；根以全新项目757b51d3fbaebc44启动。迁移gate、ready200、Worker once退出0、两个PG无主机端口均通过，所属容器/卷/网络清理后0、配置目录删除。报告`/tmp/dtc-compose-root-acceptance-report.json`，日志`/tmp/dtc-compose-root-acceptance.log`。这不是当前未提交DeepSeek代码的镜像验收，也不包含业务种子、页面或AI质量。作者构建曾遇npm/Corepack网络及工作区依赖缺失，失败日志保留于`/tmp/dtc-demo-build*.log`；受控副本修复复制依赖后实测构建通过，不修改E-pig线上接口。

独立Review未发现本候选新阻断。根25项Compose/DeepSeek/Settings聚焦测试通过（`/tmp/dtc-current-scoped-root-escalated.log`）。首次受限沙箱运行6项因绑定loopback端口被拒、19项通过；原日志保留，使用正常可绑定专用端口的授权环境按原代码重跑，没有放宽守卫。
