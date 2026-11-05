# DTC 内部售后流程：实现设计与执行计划

更新日期：2026-11-06。状态：已确认产品范围内的实现方案；2a/2b已实现并通过各章本地范围验收，Worker及页面等其余能力按下文分阶段实施，阶段结果在[实施记录](../../../records/after-sales/dtc-workflow.md)持续记录。产品行为/状态机仍以[产品蓝图](product-blueprint.md)为正本，线上E-pig字段仍以[OpenAPI](../../../../contracts/epig-after-sales/v1/openapi.json)为正本。

## 目标与分步落点

客服应能在同一诉求中查看订单和政策、形成草稿、交主管批准，再得到可核查工单。沿现有FastAPI/SQLAlchemy/React与检索内核扩展，不建通用工作流、独立审批平台或第二套Agent团队。Codex负责方案/Review/最终验收，Claude auto实现与必要测试。各阶段均提交推送并同步Issue，不扩大到真实客户/生产写入或无预算模型调用。

| 执行片段 | 交付 | 验收重点 |
|---|---|---|
| DTC-2a 身份与Connector | 员工身份/角色/市场、只读业务API、Fake/EpigConnector及契约边界、员工演示配置 | 伪造身份/跨市场/撤销权限拒绝，DTO/错误/回执符合固定源，无未授权写入入口 |
| DTC-2b 持久业务闭环 | 持久游标/去重、诉求详情、版本草稿、证据、送审/批准/驳回、操作准备与建单核验 | 单市场合成主流程，审批绑定完整版本，内部驳回无外部动作，新内容不能沿用旧批准 |
| DTC-2c Worker与恢复 | 数据库领取/租约、有限重试、未知结果对账、事件重放 | 迟到Worker不能覆盖新租约，丢回执/重启/游标边界无重复无漏单 |
| 页面与全旅程 | 客服队列/详情/审批/政策聊天，与E-pig消费者页面交替交付 | 六故事、八类UI状态、键盘可用、真实回执而非乐观假成功 |
| 评测与本地交付 | 固定评测/失败报告、隔离Compose、迁移/备份恢复、源码实验与演示 | Fake/真实模型/实际试用分别证明，未授权项保留未测 |

## DTC-2a：员工身份与只读业务边界

员工身份使用服务端保存的高熵不透明Bearer凭证：新增`after_sales_staff`表，保存员工ID、显示名、SHA-256 token摘要、启用状态、到期时间、角色集合及允许市场；不保存或返回原token。角色为support/approver/auditor/operator，权限和市场每次从数据库读取，禁用/到期/撤权立即影响新请求。该方案是有限内部试点凭证，不宣称企业SSO或MFA已实现；未来身份源更换仍复用Actor/角色授权接口，不提前搭建身份平台。

新增staff repository与FastAPI认证依赖，格式错误/未知/到期token为401，角色/市场不足403，不访问外部Connector。`GET /api/v1/after-sales/me`只返回当前员工公开身份；`GET /api/v1/after-sales/{market}/orders/{order_id}`、`.../shipment`及`.../attachments/{attachment_id}/content`供support/approver/auditor读取，operator不默认浏览订单/附件。ID/market严格校验，内容下载不返回URL或凭据。新接口统一现有安全APIErrorBody，不暴露token、原响应、地址或输入全文；现有开发期validation handler的input字段须剔除或为新接口提供等效安全处理，不能回显原body。

`after_sales_enabled=false`缺省保持现有demo兼容；关闭时新售后接口明确503/404关闭，不能匿名开放；开启后必须认证。现有chat也必须员工认证、使用服务器Actor覆盖客户端user_id、强制epig政策品牌并校验允许市场；提供已有session_id时验证归属，禁止跨员工读取会话。旧confirm_action_id在该模式拒绝，不让旧内存确认流程绕过持久审批。原上游demo只在feature关闭时保留，不能向启用后的员工流程开放通用退款/取消工具。

员工演示配置只在development/testing显式启用：独立seed脚本生成support/approver/auditor/operator和受限市场员工，随机token只写新建0600忽略文件，不打印；禁止覆盖已有凭据文件、禁止生产seed或隐式操作已有数据库。测试使用临时SQLite/专用PG和Settings(_env_file=None)，不读真实.env。上线前真实身份集成与凭据运维仍需独立验收。

## Connector契约与配置

新增`app/connectors/`的单一业务Protocol，Fake与Epig实现同一服务读写/回执接口；配置`BUSINESS_CONNECTOR=fake/epig`，缺省fake。EPIG_BASE_URL/服务SecretStr token/允许市场由服务端配置，epig缺配置失败关闭，不回退fake；production要求HTTPS，development/testing仅允许显式回环HTTP测试。不跟随重定向、不从请求体接受任意host/凭据；禁止写入自动重试。连接池/超时有上限，关闭时释放client。

Python wire模型/序列化按源OpenAPI与适用fixtures实现，不成为字段正本；service order/安全错误封闭白名单，其他允许扩展的服务对象只保留已知需要字段。发请求和接响应都核对主版本1、状态码和Schema；输入ID/幂等键遵从源pattern。Schema通过后还须核对响应market与路径、order_id/request_id/ticket_id与请求及回执的一致性；合法形状的另一对象不能视为成功。错误对象只保留安全code/retryable/recovery_action及白名单details，网络/解析失败分类，不能透传原body/URL/token。bytes附件最大5MiB且媒体类型符合契约。

EpigConnector必须覆盖service订单/物流、feed/detail、附件、建单/按ID/按键回执；create_ticket仅供后续审批执行服务使用，DTC-2a不暴露公开写API。正常create返回与重放标记、查询ticket/in-flight/暂时不可见采用明确联合结果；timeout或未知写需要LOOKUP_FIRST，不把暂时404当永久未创建。Fake提供同指纹重放、不同指纹冲突、单诉求唯一、版本守卫及可控失败；只合成数据，不把本地返回文本称为真实外部成功。

## 数据库与装配

staff表追加到现有metadata，新增Alembic0003；不把E-pig订单/工单表搬入本库。修正Alembic在线入口以支持现有asyncpg连接，并提供显式迁移目标配置（不自动读取真实.env），保持旧sqlite迁移兼容。PG启动不create_all，要求事先迁移；SQLite仅作为开发/测试便利，企业持久性场景仍要真实PG验证。

新容器属性可选/按feature装配，关闭feature不影响既有测试。create_app显式Settings必须被lifespan实际使用，避免测试设定被get_settings丢弃；新配置/身份/HTTP测试不调用真实LLM。配置和依赖变化以真实pyproject/uv.lock/CI为准，不引入当前不需要的新鉴权库。

## 2a测试计划与门槛

- staff：token只存hash，错误/过期/禁用、角色/跨市场、即时撤权拒绝，/me无摘要/token；support不能批准等后续边界留2b。
- API：实际FastAPI路由、真实签名员工凭证摘要记录、FakeConnector及受控httpx；新接口无凭证不访问Connector；chat伪造user_id/session_id不越权、epig范围/市场/旧confirm不能绕过；feature关闭的原demo回归保留。
- 契约：适用49共享正反样例，正常/错误/版本/ID/key/空白/状态联动；禁止把源正例改成负例或手改OpenAPI迁就实现。记录哪些fixture不适用于service接收端，不把未执行项算覆盖。
- Connector：httpx.MockTransport测试精确URL/头/请求体，错误白名单/敏感内容剔除、超时/无重定向/不自动重写、202与暂时404分流；Fake与Epig相同领域结果。此层不是实际跨仓HTTP验收，后续对隔离E-pig运行时执行真实请求。
- 迁移：临时空库/0002升级/重复迁移，schema对齐；PG路径在专用库验证，不读取既有DATABASE_URL。.env.example只写占位值。
- 执行锁检查、Ruff格式/静态、mypy、聚焦与全量pytest、文档/契约检查，返回真实命令/失败历史。Codex独立Review/复跑后裁决，不把实现者自测当验收。

## 后续设计约束

2b必须遵守产品7.1～7.4完整状态语义，不能用一列approved布尔代替。政策类别缺失要通过可信SKU映射或经授权的线下核验处理，不让消费者/模型自报范围；当前E-pig快照只有paid、缺真实物流时不能编造签收日期来适用退货条款。草稿、人工补充材料、政策/订单/附件/权限版本都要进入审批快照；未知外部结果先对账再处理失效，不以新key重建来“恢复”。具体表/接口在该片段开始前补充，同一主题持续维护。


## DTC-2b：持久草稿与审批（已实现、本地范围验收）

先解决一个具体问题：两名客服打开同一诉求时，不能互相覆盖草稿；主管批准后，执行内容必须与其看到的版本一致。复用现有SQLAlchemy会话、staff认证和Connector；旧聊天内存确认不适合跨重启审批，故新增专用持久业务对象，不重做通用工作流。

### 数据与并发边界

新增迁移0004及业务模块`app/services/after_sales/`、对应repository/schema/routes。表按实际生命周期划分：

| 对象 | 持久内容与约束 |
|---|---|
| case | 内部id、market/request_id唯一、服务诉求快照、order_id、内部状态、revision、material_revision、current_draft_id、最后同步时间；不把内部状态写回E-pig |
| sync_cursor | 每市场一行，原cursor、最后成功时间、revision；批次case upsert与cursor推进同事务 |
| material | 授权客服登记的内部补充/核验记录，只追加；类型、来源说明、记录人、时间，不能冒充消费者在线补聊或真实物流接口 |
| draft | case与递增version唯一；动作、内部摘要、消费者回复、证据快照、订单/附件/材料版本、作者、状态；送审后内容不可原地修改 |
| approval | draft、请求人、决定人、状态、到期、完整审批摘要与SHA-256；有效状态下同draft只能一个待处理审批 |
| operation | approval、稳定随机key唯一、规范化payload与摘要、状态、attempt、租约owner/epoch/期限、下次执行时间、已核验回执；一case同时只能一个未解决操作 |
| event | case内单调seq，类型、actor、旧/新状态、版本/操作关联及安全原因；与相应变更同事务，只追加 |

SQLite与PG都用带revision的条件UPDATE检测并发，HTTP修改请求必须带expected_revision，失败409并返回安全冲突信息。PG领取Worker时增加数据库租约，不能靠进程内锁。网络读取放在事务外，提交前重新确认revision；不持有数据库锁等待远端。同步upsert只更新远端投影，不重置内部状态、draft指针或已核验回执；记录远端sequence/updated_at并拒绝旧快照倒退；sequence沿用契约十进制字符串或可容纳完整远端范围的表示，不能使用PG int32字段缩小范围。重放旧pending页不得覆盖ticket_created/已完成结果，必要时用service detail重新核验。远端ticket_created只是投影，必须读回核验ticket与receipt才算完成；有旧操作时须同事务更新原operation/approval/draft/case。2b尚无恢复器时保持明确待核验且禁止新送审，不能仅凭feed宣称completed或抹掉执行上下文。列表摘要只用类型/附件数等结构化信息，不截取消费者自由描述。SQLite读回时间先统一UTC再比较，避免重放页因naive/aware差异失败。字段、enum与合法迁移遵从产品7.1～7.6，不允许客户端任意赋state。

### 内部API与权限

所有路径位于现有`/api/v1/after-sales/{market}`下。计划提供`GET /cases`（有界分页/状态/类型/时间筛选）、`GET /cases/{case_id}`及`.../events`；support/approver/auditor可以读取本市场；operator只用独立脱敏运行视图，不经此入口读取材料或回复。列表不返回描述全文和凭据。对象查询必须同时限定market和id。

support可`POST /cases/sync`拉取一页（后续由Worker持续轮询），`POST /cases/{id}/materials`、`POST .../drafts`新版本、`POST .../drafts/{version}/submit`送审。approver可`POST .../approvals/{approval_id}/approve`或`.../reject`；同时拥有两角色也禁止批准本人发起的申请。内部驳回必须原因，仅返回processing，不创建操作、不通知消费者。auditor只读。同步跨市场、operator业务写入、客户端传staff/approved/receipt或任意Connector参数均拒绝。

批准只持久化approved及prepared操作，不在HTTP决定请求中调用create_ticket。执行由2cWorker完成；2b没有公开“直接建单”接口。HTTP重复批准只能返回同一结果或409，不能准备第二个操作。审批ID与draft/action/reply/order_version/policy_version共同生成固定wire payload；审批记录与操作事务一起提交。尚未派遣的prepared若因过期、撤权或版本变化不能执行，必须在确认dispatch从未发生后同事务置failed、保留安全原因/event并释放活跃守卫，允许重新送审；产品7.4已明确此迁移。已派遣或未知的操作不走此路径。

### 证据与审批失效

政策不是可自由填写的版本字符串。复用已有policy manifest、实际chunk和corpus_fingerprint，服务端按E-pig品牌、market和可信商品类别选取条款，保存文档/chunk id、版本、来源路径、原文、hash、服务器判定as_of、生效/失效日期和demo标记；复用已有PolicyScope日期规则，过期/未来/适用日不明的证据不能送审。支持的SKU类别由显式服务端配置映射；无映射时进入needs_info，不信任消费者或模型的商品类别。授权客服可以登记有来源的线下核验材料，由审批人审阅；它与真实订单/物流字段分开，不能覆盖Connector快照。缺签收证明不适用依赖签收日期的退货期限判断。动作必须匹配实际政策主题：退货对应returns，退款对应refunds，破损/错发补发对应faults-missing；reject/other必须有明确相关条款及人工核验说明，不能只选任意同市场chunk。涉及退货期限时，采用真实shipment已交付事件，或授权客服以明确类型、商品关联、实际交付日期及来源登记的线下核验；自由文本internal_note不充当签收证明。所需照片/商品状态等由带来源的人工核验记录说明已检查，纳入审批快照，不伪称代码能从照片自动判定资格。必要材料缺失进入needs_info，审批人不能绕过；不新增通用规则DSL。

draft保存人工编辑内容，送审时必须有足够且不冲突的匹配政策/材料；服务端重算订单关键版本、附件id/状态/到期、材料revision及政策内容指纹。摘要包含全部执行字段、case/market/order/request、申请人、授权范围、批准到期与审批人决定身份。批准前再读实际订单、诉求及附件当前状态，并比较当前staff权限与所有快照；任一不符明确invalidated并返工。执行前仍须再验，不能用批准时查询代替执行时查询。

新材料/新草稿使未派遣的旧待审或批准失效/被取代并记录事件。若旧operation已派遣/结果未知，保留旧draft/approval执行上下文，先对账；可以保存新候选但不能送审准备第二个写入。找到旧回执后按原版本consumed，case completed且禁止再建单。

AI建议与人工审批分离：2b先交付手工草稿及真实证据选择，后续复用现有LLM/检索生成建议，仅形成新draft，不授予批准权。默认离线LLM，不新增付费调用；缺证据时拒绝生成确定业务承诺。此拆分不缩减最终AI辅助需求。

### 2b测试门槛

实际FastAPI/SQLite验证同步去重、跨市场隔离、分页/筛选、materials追加、草稿版本不可覆盖、过期/撤权/自批拒绝、内部驳回无外部写、批准只准备一个操作、订单/政策/附件/材料变化失效；并发相同revision只有一方成功。临时库迁移从0003升级与空库重复运行，真实PG事务/唯一约束在专用容器复验。另测prepared批准失效后可安全重审，以及已完成case重放旧pending feed不会退化。FakeConnector记录create调用数必须为零。每次状态变动有同事务event，失败回滚不留下孤立审批/操作或推进游标。保持2a回归和全量静态/测试。

## DTC-2c：执行、租约与恢复（实施前约束）

Worker作为同一代码库的独立进程，不引入Celery或消息代理。周期轮询已配置市场并领取prepared/到期重试/需对账操作；有界批次、超时和退避，退出时关闭Connector/DB。每次领取持久owner/epoch/lease_until，所有提交以owner+epoch+状态条件更新。租约不是撤销外部调用：过期的dispatching先对账，不能直接重发。

第一次派遣前验证批准、权限、所有业务版本；持久dispatching/attempt/event后才发同key同payload。成功响应仍经按ID/按键读回并核验全部审批字段和receipt；匹配才原子succeeded/consumed/completed。写超时、丢响应、读回暂404、IN_FLIGHT或未知协议错误进入reconciliation_required，不能标普通失败或换key。恢复时即使批准过期也先查原结果；查到则消耗旧批准。只有契约允许同key重试且当前批准/版本仍有效才进入retry_wait；次数或对账时间上限后保留人工待核验，不盲目把unknown转failed。

operator只能请求继续查询、查看脱敏进度及登记有来源的对账材料，不能输入“成功”或伪造receipt。写入事实以Connector已核验回执为准。单case活跃操作守卫跨重启有效；迟到Worker即使收到成功也不能覆盖新epoch，后续领取者可查原键恢复同一结果。

验收必须包含专用PG真实并发领取/租约过期、外部成功本地未提交、进程重启、原键恢复、迟到Worker、过期批准先对账、暂404与IN_FLIGHT、cursor批次失败回滚、重复feed无漏单；记录Fake/真实跨仓HTTP/PG层次，不能用单进程Fake替代PG并发证明。


## 2a源码阅读与小实验

当前未提交候选的读取链是`after_sales.py`路由 → `get_required_staff` → `StaffRepository.authenticate` → `BusinessConnector`。Bearer是每个HTTP请求携带的员工凭证；数据库只保存摘要，类似前端拿到会话凭证后每次请求带上，但“谁能读取哪个市场”始终由服务器判断。浏览器传user_id或隐藏按钮都不能替代该判断。

先读[认证依赖](../../../../app/api/dependencies.py)与[员工仓储](../../../../app/repositories/staff_repository.py)，再读[只读路由](../../../../app/api/routes/after_sales.py)与[Connector接口](../../../../app/connectors/base.py)。Fake实现用于合成试验；Epig实现通过HTTP访问固定契约，MockTransport测试只证明请求/解析逻辑，不证明真实服务可用。

小实验在仓库根目录执行`.venv/bin/pytest -q tests/unit/test_staff_repository.py tests/integration/test_after_sales_api.py`。预期：不存在/过期/禁用凭证被拒绝，support不能访问未授权市场，operator不能下载业务附件；将员工禁用后下一次请求立即拒绝。实际结果须看本章验收记录，不能把这里的预期当通过计数。迁移实验使用临时库，见`tests/integration/test_after_sales_migrations.py`；PG测试缺专用URL会skip，这表示未测。

阅读后应能回答：为什么只存hash仍要保护原token；为什么市场限制要在路由和Connector两端校验；为什么协议Schema合法还要核对对象ID；为什么PG启动不执行create_all；为什么这些只读能力尚不足以证明审批与恢复已完成。


## 后续AI建议与员工工作台（实施前设计）

在2b/2c基础行为验收后实施。员工不应复制数据库ID才能选证据，因此计划增加本case上下文的只读evidence入口：服务器从当前订单SKU映射、市场和政策日期派生scope，返回真实可用chunk/来源/版本与缺资料原因。support/approver/auditor按同一市场授权，不接受客户端policy_version或商品范围覆盖。该内部读取不新增E-pig wire操作。

AI建议入口仅support可用，带expected_revision；复用已有检索与LLMProvider，关闭通用业务工具。网络/模型调用在事务外，固定服务器订单/材料/政策上下文；候选action/reply/summary及引用需白名单校验，引用必须属于本次真实匹配chunk。模型内容不改变身份、材料真伪、审批或操作。成功只保存一个新的editing草稿版本，保存前复用业务校验重读staff角色/市场并比较生成时订单/材料/附件/政策指纹，再次CAS；即使case revision未变，权限变化也403、业务上下文变化409，不保存候选且不覆盖；无证据/冲突/模型故障明确返回未生成。默认OfflineLLM/Fake，真实模型评测未授权前不调用。建议仍经过人工修改、送审、不同人批准与Worker核验，不自动形成工单。

前端复用现有React/TypeScript/Vite、CSS变量和lucide，采用独立员工工作台入口，不引入router/状态管理/UI框架。员工凭证只在内存，刷新重新认证；/me提供角色和允许市场。退出、401/403或身份/市场切换时清除详情、附件blob、草稿和聊天内存，并中止旧请求，避免迟到响应跨身份回填。业务文本/回复/凭证不保存localStorage；原上游demo聊天的本地历史不混入员工模式。

工作台按“队列→详情→审批/执行结果”组织：队列有市场/状态/类型/时间筛选及最后同步时间；详情展示订单快照的查询时间、消费者材料/内部补充的区别、授权附件、真实政策原文、AI/人工草稿版本和事件时间线。support能补资料/保存新版本/送审；approver能查看完整冻结快照及批准/内部驳回原因；自批/越权/旧revision以服务端结果为准。operator只显示脱敏执行/对账面板，不能走业务详情读取PII。无直接“绕过审批建单”按钮，批准成功与工单已核验分开显示。

加载/空数据/无权限/错误/陈旧/冲突/有限重试/成功均有明确文字，关键结果可在时间线再次读取。409保存用户未提交输入并引导查看最新版本，不静默覆盖；附件object URL在切换/退出撤销。政策聊天复用低层SSE解析与现有回答/引用组件，但独立内存会话并携带员工认证，不沿用持久本地历史hook。界面只展示demo政策声明与实测状态，不展示内部工具实现细节或虚构采用率。

验收含API角色/引用造假/无证据/模型失败/生成期间revision变化，以及revision未变但订单/政策/权限变化、AI不触发create、前端构建与相关测试、实际浏览器键盘/窄屏/角色切换/409/权限撤销/完整审批与回执旅程；jsdom或Fake结果不代替实际浏览器与跨仓HTTP。界面通过后再做隔离Compose与演示脚本，不先搭复杂发布平台。


### 2c执行落点与可恢复边界

Worker实现独立`app/services/after_sales/execution.py`和进程入口，复用2b业务校验、规范payload与repository；不继续把执行循环堆入审批service，也不重写整个业务模块。首次需要的lookup计数/对账状态可由0005最小追加到operation，不建通用任务/对账平台。审批和草稿响应补产品既有executing/consumed等状态，不能以输出模型遗漏导致核验后500。

领取在数据库原子条件更新owner/epoch/lease_until；条件含可领取状态和期限，使用数据库时钟或统一可验证UTC，不凭进程内锁。完成、错误、重试和租约释放均带当前owner+epoch+状态条件；过期派遣的领取者先lookup。每次外部尝试的状态/event先提交，数据库事务不跨网络await。单次命令可执行有限一轮，持续循环有配置上限和有界退出，取消不把已发写标成未发生。

prepared派遣前重验申请人/审批人仍启用、未过期、角色/市场、自批分离、批准期限、case/draft/material及订单/附件/政策/交付证明。使用现有2b校验，新增公共窄接口即可，不复制整个校验器。失败明确从未派遣时原子failed/expired或invalidated并释放活跃限制；已经dispatching/unknown则先查回执，不先归档旧批准。

核验以operation持久payload及其绑定的原始不可变approval snapshot为准；wire payload没有market/order_id，这两项取原始snapshot的case.market/order.order_id，不能用当前可被同步更新的case替代。核对market/order/request、action/reply、draft/approval/policy/order版本及receipt内部关联均匹配。创建201/重放200仍经GET读回；未知写协议错误与timeout都lookup-first。只在匹配后同事务operation succeeded、原draft/approval consumed、case completed与ticket投影/event。case revision因保存新candidate变化不应抹掉已发生的旧结果；完成事务读取当前revision并条件提交，不能消耗新candidate或重新计算原wire参数。

lookup为IN_FLIGHT或暂不可见时保持未知，次数与时间均有限退避。RETRY_CREATE_WITH_SAME_KEY只允许当前原批准/上下文仍有效时回到retry_wait，永远原key原payload，不能清空指纹或新建operation。重试上限/信息不足进入manual_review且停止自动派遣，保留可审计记录。超过期限不证明外部失败；真正明确无外部效果的拒绝才failed。reconciliation状态复用operation字段及event历史表达产品7.6，不新增另一个队列。

远端观察已有ticket但无可匹配本地operation/批准链时，可以读取验证外部事实，但不能伪造本地审批或把不匹配结果消耗为本操作成功；保持人工核验并封锁新写。operator只经脱敏运行列表/详情及“继续查询”入口推进查证，不能提交成功标志或自造receipt。排队重试不得改变payload/key，日志与metrics标签不包含回复/描述、认证或原请求体。

交付仓内专用PG runner，生命周期参考已验证E-pig方案：新随机命名/端口/密码容器、最终TCP目标SQL就绪、显式0001～head迁移、仅传本次连接、子进程组有界结束、finally只删本容器/卷。默认pytest仍离线不隐式启动Docker，专用入口缺Docker明确失败；将2b现有workflow测试的专用PG适配由临时验收产物收敛到可重跑入口，生产实现不因测试关闭pool或改变事务。

2c验证至少包括：两个真实PG连接竞争领取（确认不同session/有界屏障）、租约过期与迟到提交、外部成功但本地结果落库失败、同键同payload恢复、IN_FLIGHT/暂404/失效批准先对账、有效批准的有限重试、错误回执拒绝、无本地批准链转人工、事件重放/游标事务回滚。Fake用于可控故障，实际跨仓HTTP/进程重启在两仓独立合成库执行，不用给Fake新增一套生产持久平台。CLI/health/操作指南仅在入口实际存在并运行后更新，不预填性能/SLA或真实试用结果。
