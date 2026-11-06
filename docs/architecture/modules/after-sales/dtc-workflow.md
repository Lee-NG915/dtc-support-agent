# DTC 内部售后流程：实现设计与执行计划

更新日期：2026-11-06。状态：已确认产品范围内的实现方案；2a/2b、2c执行核心及员工证据/建议后端已实现并通过各章本地范围验收，跨仓HTTP、页面及其余能力按下文分阶段实施，阶段结果在[实施记录](../../../records/after-sales/dtc-workflow.md)持续记录。产品行为/状态机仍以[产品蓝图](product-blueprint.md)为正本，线上E-pig字段仍以[OpenAPI](../../../../contracts/epig-after-sales/v1/openapi.json)为正本。

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

Worker作为同一代码库的独立进程，不引入Celery或消息代理。每轮先对配置服务市场各拉取有界一页E-pig feed，复用2b原子游标；服务接收actor_staff_id为空并注明service poll，不伪造员工身份。单市场失败不推进游标，记录安全错误并继续其他市场和已有批准操作。员工手工同步是补充，不能替代后台拉取。随后领取prepared/到期重试/需对账操作；有界批次、超时和退避，退出时关闭Connector/DB。每次领取持久owner/epoch/lease_until，所有提交以owner+epoch+状态条件更新。租约不是撤销外部调用：过期的dispatching先对账，不能直接重发。

第一次派遣前验证批准、权限、所有业务版本；持久dispatching/attempt/event后才发同key同payload。成功响应仍经按ID/按键读回并核验全部审批字段和receipt；匹配才原子succeeded/consumed/completed。写超时、丢响应、读回暂404、IN_FLIGHT或未知协议错误进入reconciliation_required，不能标普通失败或换key。恢复时即使批准过期也先查原结果；查到则消耗旧批准。只有契约允许同key重试且当前批准/版本仍有效才进入retry_wait；次数或对账时间上限后保留人工待核验，不盲目把unknown转failed。

operator只能请求继续查询、查看脱敏进度及登记有来源的对账材料，不能输入“成功”或伪造receipt。写入事实以Connector已核验回执为准。单case活跃操作守卫跨重启有效；迟到Worker即使收到成功也不能覆盖新epoch，后续领取者可查原键恢复同一结果。

验收必须包含专用PG真实并发领取/租约过期、外部成功本地未提交、进程重启、原键恢复、迟到Worker、过期批准先对账、暂404与IN_FLIGHT、cursor批次失败回滚、重复feed无漏单；记录Fake/真实跨仓HTTP/PG层次，不能用单进程Fake替代PG并发证明。


## 2a源码阅读与小实验

当前未提交候选的读取链是`after_sales.py`路由 → `get_required_staff` → `StaffRepository.authenticate` → `BusinessConnector`。Bearer是每个HTTP请求携带的员工凭证；数据库只保存摘要，类似前端拿到会话凭证后每次请求带上，但“谁能读取哪个市场”始终由服务器判断。浏览器传user_id或隐藏按钮都不能替代该判断。

先读[认证依赖](../../../../app/api/dependencies.py)与[员工仓储](../../../../app/repositories/staff_repository.py)，再读[只读路由](../../../../app/api/routes/after_sales.py)与[Connector接口](../../../../app/connectors/base.py)。Fake实现用于合成试验；Epig实现通过HTTP访问固定契约，MockTransport测试只证明请求/解析逻辑，不证明真实服务可用。

小实验在仓库根目录执行`.venv/bin/pytest -q tests/unit/test_staff_repository.py tests/integration/test_after_sales_api.py`。预期：不存在/过期/禁用凭证被拒绝，support不能访问未授权市场，operator不能下载业务附件；将员工禁用后下一次请求立即拒绝。实际结果须看本章验收记录，不能把这里的预期当通过计数。迁移实验使用临时库，见`tests/integration/test_after_sales_migrations.py`；PG测试缺专用URL会skip，这表示未测。

阅读后应能回答：为什么只存hash仍要保护原token；为什么市场限制要在路由和Connector两端校验；为什么协议Schema合法还要核对对象ID；为什么PG启动不执行create_all；为什么这些只读能力尚不足以证明审批与恢复已完成。


## AI建议与员工工作台（后端已验收，前端工程检查通过、浏览器待验）

在2b/2c基础行为上补充后端，前端已按下文实现并通过工程检查，实际浏览器和全旅程继续验收。员工不应复制数据库ID才能选证据，因此已增加本case上下文的只读evidence入口：服务器从当前订单SKU映射、市场和政策日期派生scope，返回真实可用chunk/来源/版本与缺资料原因。support/approver/auditor按同一市场授权，不接受客户端policy_version或商品范围覆盖。该内部读取不新增E-pig wire操作。详情结果还需要本case的只读ticket入口：按已关联远端ID经现有Connector读真实工单，核验market/order/request及receipt，有本地operation时继续匹配原payload。仅support/approver/auditor可读；operator不能借此看回复。读取不伪造本地批准或推进完成，返回查询时间；失败时UI只能把此前已核验回执标陈旧，不能当最新履行状态。复用源ServiceTicket，不新增E-pig接口。

AI建议入口仅support可用，带expected_revision；复用已有检索与LLMProvider，关闭通用业务工具。网络/模型调用在事务外，固定服务器订单/材料/政策上下文；候选action/reply/summary及引用需白名单校验，引用必须属于本次真实匹配chunk。模型内容不改变身份、材料真伪、审批或操作。成功只保存一个新的editing草稿版本，保存前复用业务校验重读staff角色/市场并比较生成时订单/材料/附件/政策指纹，再次CAS；即使case revision未变，权限变化也403、业务上下文变化409，不保存候选且不覆盖；无证据/冲突/模型故障明确返回未生成。默认OfflineLLM/Fake，真实模型评测未授权前不调用。建议仍经过人工修改、送审、不同人批准与Worker核验，不自动形成工单。

已实现三个内部入口，复用现有case路径，不增加E-pig wire操作：

| 内部入口 | 输入与结果 | 守卫 |
|---|---|---|
| `GET /after-sales/{market}/cases/{case_id}/evidence` | 当前可信范围、真实政策chunk及来源/版本、查询时间、缺资料原因 | support/approver/auditor；市场、SKU类别与日期由服务端派生，禁止客户端扩大范围 |
| `GET /after-sales/{market}/cases/{case_id}/ticket` | 实际远端工单、查询时间；尚无工单明确未找到 | 同上；核对case身份、receipt及存在的原operation payload，读取不改审批或完成状态 |
| `POST /after-sales/{market}/cases/{case_id}/suggestion` | 仅expected_revision；成功返回保存后的case详情 | support；有界结构化生成、严格候选/引用验证，保存前再次读取员工与业务指纹并CAS |

完整路径含既有`/api/v1`前缀。模型上下文只包含该case必要事实和已过滤的政策，不传凭证、其他市场或业务写工具；消费者文本按资料处理。复用`LLMProvider.generate(response_format={"type":"json_object"})`，非法JSON/未知字段/动作/伪引用返回安全错误，不自动修补成可审批内容。运行时`OfflineLLMProvider`未配置生成模型时明确提示不可用并允许人工草稿，不把测试JSON展示为AI建议；注入Fake用于工程验收，真实质量保留未测。生成期间不持有数据库事务；保存前复用`StaffRepository.get_current_actor`及当前源数据核验。无需新增模型代理框架、表或工作流引擎。

建议成功必须至少有一条本次有效引用，不能让空集合通过子集检查。审批人需看到送审时冻结的依据：在既有授权case详情的ApprovalView增加白名单化snapshot展示，包含当时的草稿、订单、材料、证据/版本、权限与到期依据；从已存不可变snapshot读取，不以当前case拼装，不返回凭据或执行密钥。operator仍无该业务详情权限。这是当前摘要字段不足的最小补充，不另建审批系统。

前端复用现有React/TypeScript/Vite、CSS变量和lucide，采用独立员工工作台入口，不引入router/状态管理/UI框架。员工凭证只在内存，刷新重新认证；/me提供角色和允许市场。退出、会话/市场失效的401/403或身份/市场切换时清除详情、附件blob、草稿和聊天内存，并中止旧请求，避免迟到响应跨身份回填。业务文本/回复/凭证不保存localStorage；原上游demo聊天的本地历史不混入员工模式。

员工入口采用`/staff`，由现有main入口按路径选择独立组件，旧demo不共用会话hook。工作台使用队列/详情两列，窄屏按队列→详情顺序展示；顶栏显示当前员工、角色、市场、退出及数据查询时间。结果以服务端重新读取为准；批准后可有界轮询执行状态或手动刷新，不能把按钮成功当成已履行。审批界面展示冻结快照与当前变更提示，operator仅见脱敏运行列表/事件及继续查询。附件使用员工认证下载的blob预览，并在切换/退出撤销URL。

工作台按“队列→详情→审批/执行结果”组织：队列有市场/状态/类型/时间筛选及最后同步时间；详情展示订单快照的查询时间、消费者材料/内部补充的区别、授权附件、真实政策原文、AI/人工草稿版本和事件时间线。support能补资料/保存新版本/送审；approver能查看完整冻结快照及批准/内部驳回原因；自批/越权/旧revision以服务端结果为准。operator只显示脱敏执行/对账面板，不能走业务详情读取PII。无直接“绕过审批建单”按钮，批准成功与工单已核验分开显示。

加载/空数据/无权限/错误/陈旧/冲突/有限重试/成功均有明确文字，关键结果可在时间线再次读取。409保存用户未提交输入并引导查看最新版本，不静默覆盖；附件object URL在切换/退出撤销。政策聊天复用低层SSE解析与现有回答/引用组件，但独立内存会话并携带员工认证，不沿用持久本地历史hook。界面只展示demo政策声明与实测状态，不展示内部工具实现细节或虚构采用率。

验收含API角色/引用造假/无证据/模型失败/生成期间revision变化，以及revision未变但订单/政策/权限变化、AI不触发create、前端构建与相关测试、实际浏览器键盘/窄屏/角色切换/409/权限撤销/完整审批与回执旅程；jsdom或Fake结果不代替实际浏览器与跨仓HTTP。界面通过后再做隔离Compose与演示脚本，不先搭复杂发布平台。


### 2c执行落点与可恢复边界

Worker实现独立`app/services/after_sales/execution.py`和进程入口，复用2b业务校验、规范payload与repository；不继续把执行循环堆入审批service，也不重写整个业务模块。首次需要的lookup计数/对账状态可由0005最小追加到operation，不建通用任务/对账平台。审批和草稿响应补产品既有executing/consumed等状态，不能以输出模型遗漏导致核验后500。

领取在数据库原子条件更新owner/epoch/lease_until；条件含可领取状态和期限，使用数据库时钟或统一可验证UTC，不凭进程内锁。完成、错误、重试和租约释放均带当前owner+epoch+状态条件；过期派遣的领取者先lookup。每次外部尝试的状态/event先提交，数据库事务不跨网络await。单次命令可执行有限一轮，持续循环有配置上限和有界退出，取消不把已发写标成未发生。

prepared派遣前重验申请人/审批人仍启用、未过期、角色/市场、自批分离、批准期限、case/draft/material及订单/附件/政策/交付证明。使用现有2b校验，新增公共窄接口即可，不复制整个校验器。失败明确从未派遣时原子failed/expired或invalidated并释放活跃限制；已经dispatching/unknown则先查回执，不先归档旧批准。

核验以operation持久payload及其绑定的原始不可变approval snapshot为准；wire payload没有market/order_id，这两项取原始snapshot的case.market/order.order_id，不能用当前可被同步更新的case替代。核对market/order/request、action/reply、draft/approval/policy/order版本及receipt内部关联均匹配。创建201/重放200仍经GET读回；未知写协议错误与timeout都lookup-first。只在匹配后同事务operation succeeded、原draft/approval consumed、case completed与ticket投影/event。case revision因保存新candidate变化不应抹掉已发生的旧结果；完成事务读取当前revision并条件提交，不能消耗新candidate或重新计算原wire参数。

lookup为IN_FLIGHT或暂不可见时保持未知，次数与时间均有限退避。RETRY_CREATE_WITH_SAME_KEY只允许当前原批准/上下文仍有效时回到retry_wait，永远原key原payload，不能清空指纹或新建operation。重试上限/信息不足进入manual_review且停止自动派遣，保留可审计记录。超过期限不证明外部失败；真正明确无外部效果的拒绝才failed；若此前已有未知尝试，后一次401/400等只证明该次被拒绝，不能据此释放整笔操作的活跃限制，仍须核验原结果或转人工。原批准过期/撤权时只停止新派遣，未知历史不能被改写成从未发生。reconciliation状态复用operation字段及event历史表达产品7.6，不新增另一个队列。

远端观察已有ticket但无可匹配本地operation/批准链时，可以读取验证外部事实，但不能伪造本地审批或把不匹配结果消耗为本操作成功；保持人工核验并封锁新写。operator只经脱敏运行列表/详情及“继续查询”入口推进查证，不能提交成功标志或自造receipt。排队重试不得改变payload/key，日志与metrics标签不包含回复/描述、认证或原请求体。

交付仓内专用PG runner，生命周期参考已验证E-pig方案：新随机命名/端口/密码容器、最终TCP目标SQL就绪、显式0001～head迁移、仅传本次连接、子进程组有界结束、finally只删本容器/卷。默认pytest仍离线不隐式启动Docker，专用入口缺Docker明确失败；将2b现有workflow测试的专用PG适配由临时验收产物收敛到可重跑入口，生产实现不因测试关闭pool或改变事务。

2c验证至少包括：两个真实PG连接竞争领取（确认不同session/有界屏障）、租约过期与迟到提交、外部成功但本地结果落库失败、同键同payload恢复、IN_FLIGHT/暂404/失效批准先对账、有效批准的有限重试、错误回执拒绝、无本地批准链转人工、事件重放/游标事务回滚。Fake用于可控故障，实际跨仓HTTP/进程重启在两仓独立合成库执行，不用给Fake新增一套生产持久平台。CLI/health/操作指南仅在入口实际存在并运行后更新，不预填性能/SLA或真实试用结果。


## 评测与本地交付窗口（实施前方案）

评测先分清要证明的事：工程测试验证权限、事务与恢复；Fake评测验证流程和评分器可运行；真实模型评测才讨论输出/引用质量。现有34条政策固定集作为开发回归，不能冒称完整Agent/保留集；旧e2e16条为upstream_demo问答，答案中出现订单号也不等于实际工具调用正确。上游MEASURED数据不转为本fork指标。

当前代码审查发现旧`generation/e2e`任务不带`--llm`仍会调用模型，e2e还读取环境并seed；在隔离/显式真实模型开关改造前不执行。新入口默认Fake/离线、Settings(_env_file=None)/专用数据，不使用现有DB或凭据；真实模型必须显式Provider/模型与累计预算/凭据配置，预算未确认不新增收费调用。既有历史真实生成节点结果保留来源边界，不外推完整图或Faithfulness已测。

最小集合：固定10个合成种子案例绑定六故事，另加内部驳回vs正式reject、签收证明不足、并发审批和未知中候选修改。保留34政策开发回归；在prompt/知识/参数冻结后运行12条独立保留样例，包含政策、多条件订单上下文、草稿、无证据/注入与恢复边界。保留集不参与调参，若据其失败修改方案，它即成为回归集，下一轮另建未用样例，不靠删例美化通过率。

10个开发/演示种子的固定组织如下，编号仅为合成场景标识，不新增线上枚举或通用场景DSL。执行脚本复用实际API与已有fixture，不直接SQL伪造审批/成功：

| 种子 | 故事与触发 | 可观察结果 |
|---|---|---|
| DEV-01 | 正常破损，合法图片和适用政策，人工材料充分 | 不同员工审批后唯一工单/回复，回执读回；只表示建单，不表示补发已履行 |
| DEV-02 | 退货缺签收核验或当前状态凭证 | needs_info/人工补资料，外部零写入 |
| DEV-03 | US员工读AU案例、消费者换订单/凭据 | 服务端拒绝；受限详情/附件不暴露 |
| DEV-04 | prepared批准后保存新版本或政策/订单改变 | 原批准/操作失效且零派遣，须新版本重新审批 |
| DEV-05 | 源建单已提交但响应未达、本地回执未存 | 原key查询恢复同一结果，累计POST/工单/回复不重复 |
| DEV-06 | 等待审批时重启，或游标提交边界崩溃 | 冻结草稿/批准仍可读；重读去重且不漏诉求 |
| DEV-07 | 主管内部驳回并给出原因 | 仅内部返工，无外部工单/消费者回复 |
| DEV-08 | 有人工复核依据的正式reject草稿 | 仍需批准，形成唯一正式拒绝工单与回复 |
| DEV-09 | 两次审批/两个Worker竞争 | 一笔prepared/一个有效领取者，旧epoch不能提交 |
| DEV-10 | 未知结果期间保存新候选 | 原执行指针和批准保留，新候选可见但不可第二次送审写入 |

这些是开发回归与演示样例，不是12条保留集。保留集由验收负责人在生产prompt/知识冻结后另行准备，覆盖不同表达和事实组合；报告逐例区分确定性权限/状态守卫、Fake plumbing与实际模型输出质量，不能把混合集合的通过率当LLM准确率。当前真实模型配置/预算未确认，只有离线结果可以运行。

报告至少记录源提交/数据digest、Fake或Provider/模型、提示词/知识版本、每例期望与禁止行为、实际工具名/参数/调用次数、引用支持性/转人工、业务状态/回执、失败分类、成本和延迟（仅实测）。人工按事实正确、证据充分和可执行性逐例裁决，不能只靠关键词自评。缺模型或实际试用时明确未测，不计算虚构效率提升。完整图与Faithfulness必须有自己的运行证据，不能由生成节点22例或确定性门控12例拼出模型全链路通过。

交付复用Docker/现有应用定义，目标是两仓独立合成数据库、显式迁移、API/Worker/前端、健康与版本配置可核对；不共享业务表、不新增微服务/队列。补干净安装/升级、备份还原、受控故障恢复、日志脱敏和演示脚本，保留实际命令/失败/未测。生产部署、真实客户试用和未授权付费评测不在本次默认授权内；本地完整演示与真实采用分别验收。


## 从送审到prepared：2b源码与小实验

客服修改回复后，主管先前的批准为什么不能继续用？因为批准针对的是“这一版材料和这一段回复”。这里的快照就是把审批时看到的订单、附件、政策、人工核验与草稿版本冻结保存；摘要用于发现版本差异，不是代替真实身份或给模型授予权限。

先读[内部路由](../../../../app/api/routes/after_sales_workflow.py)的submit_draft/approve，再读[业务服务](../../../../app/services/after_sales/service.py)的submit_draft/approve与_approval_problems，最后看[仓储](../../../../app/repositories/after_sales_repository.py)的cas_case/advance_cursor。条件更新类似前端提交表单时带版本号，但正确性来自数据库`WHERE revision=...`只有一方能更新，而不是浏览器按钮被禁用。游标推进和本批诉求同事务，因此失败不能只推进游标把未保存记录跳过去。

批准返回prepared只说明参数已冻结、等待执行，不表示工单存在。[payload模块](../../../../app/services/after_sales/payload.py)从原审批快照生成wire字段，不能让页面再传一份随时变化的参数。实际建单与回执由2c负责；内部驳回只把工作退给客服，不发送消费者回复。

在根目录运行`.venv/bin/pytest -q tests/integration/test_after_sales_workflow.py -k 'approval_prepares or internal_rejection or concurrent_empty_sync'`。预期：批准只准备一笔操作且Fake create调用0；内部驳回无外部工单；两次空页同步只一方推进revision。这条默认命令使用临时SQLite，不当作PG成绩，真实PG独立结果见实施记录；仓内专用入口见下节。阅读后应能解释为何未知结果不换key、为何新candidate不能覆盖旧执行版本、为何政策或签收材料变化需要重新审核。

## 从prepared到回执：2c源码与小实验

Worker在发送建单后被杀，数据库可能只知道“已经开始派遣”。租约是一次领取的有效期限，epoch是领取版本：旧进程即使迟到，也不能覆盖新领取者的结果；它们不能撤回已经发出的HTTP，因此恢复仍要用原key查询真实工单。

先读[Worker入口](../../../../app/worker/main.py)的ContainerWorkerRuntime/run_worker_loop，看到每市场有界同步、执行和资源关闭；再读[执行服务](../../../../app/services/after_sales/execution.py)的_claim_due、_dispatch、_lookup、_complete。派遣前检查批准与当前材料，未知后保留原批准/payload进行查询；找到回执后核对对象和内容，同事务消费原草稿/批准并记录完成。operator的lookup只继续查询，不重新授权POST；manual_review保持人工边界。

仓库根目录运行`.venv/bin/python -m app.worker --help`可查看实际参数，不会派遣。仅在明确配置的专用合成环境、显式迁移后运行`--once --markets au`；Worker读取运行配置，不能把这条派遣参数直接用于已有业务库的小实验。无需业务凭据的数据库实验使用`.venv/bin/python scripts/run_after_sales_pg.py`：需Docker与项目依赖，会自行新建随机PG容器、运行固定测试并清理所属资源，不接收任意库URL/命令，也不读取.env。

预期观察三组真实进程/backend：竞争只一个owner，过期重领epoch增加且旧提交被拒，进程被杀后租约仍可恢复。报告中的pg_adapted/pg_concurrency/pg_migration与sqlite_migration分别计数；默认pytest的PG skip不是通过。实际版本、结果、失败历史与跨仓/浏览器边界见[实施记录](../../../records/after-sales/dtc-workflow.md)。阅读后应能说明：为什么需要外部幂等、为什么未知时不能换key、为什么后来一次明确拒绝不能抹去更早未知、为什么批准与completed是不同事实。


## 从AI建议到人工草稿：员工后端源码与小实验

模型返回一段看似合理的回复，为什么不能直接批准？回复只是一份候选，它可能引用别的市场条款，生成期间员工也可能被撤权或材料已变化。服务端先固定当前诉求和真实政策，再严格检查动作、文本和非空引用；保存时再核对权限、日期和版本。这个边界类似前端表单带revision提交，但完整校验在服务端，模型不决定权限。

先读after_sales_workflow路由的三个case子路径，再读[流程服务](../../../../app/services/after_sales/service.py)的get_case_evidence、get_case_ticket、generate_suggestion/_revalidate_suggestion/_save_suggestion。模型和远端查询不占数据库事务；最后保存事务重读员工权限并CAS，只新增editing。冻结审批展示从已存snapshot白名单读取；[共享核验](../../../../app/services/after_sales/verification.py)比较真实ticket与原批准payload，不推进本地状态，也不复制Worker。

根目录运行`.venv/bin/pytest -q tests/integration/test_after_sales_workbench.py`。预期正常Fake建议仅建草稿，未配置模型明确不可用，伪引用/空引用被拒；在生成后的源查询期间撤权得到403，跨UTC日得到409，原批准/操作不被无效候选破坏。此命令默认临时SQLite，不调用收费模型；真实PG使用前述仓内专用入口，已包含该模块并核验实际engine。实际结果见实施记录，不把测试Fake的输出当模型质量。

阅读后应能解释：为什么模型前认证不足；为什么已有case revision还需读源指纹和政策日期；为什么审批snapshot不能用当前case拼；为什么读取外部ticket不等于替它补造本地批准。员工UI、真实模型质量与完整旅程继续分别验收。


## 员工工作台工程入口与源码实验

`/staff`由[前端入口](../../../../frontend/src/main.tsx)选择独立StaffApp，Vite开发与有构建产物的API静态服务均提供该shell；无dist时API仍可独立运行。shell公开显示登录表单，订单/材料/审批数据仍由每个API的员工认证与市场权限控制，不能靠隐藏按钮授权。真实浏览器与完整旅程尚未验收，工程门禁不代替这两个结果。

先读[StaffApp](../../../../frontend/src/staff/StaffApp.tsx)与[RequestGuard](../../../../frontend/src/staff/guard.ts)，再读DraftSection和ApprovalSection：generation绑定当前身份/市场，请求取消后迟到结果仍须检查；baseRevision是编辑依据的服务端版本，自动刷新不代表用户看过新版；编辑序号保护“保存A期间输入B”不能被A的回包误标已保存。审批内容来自冻结snapshot，批准后仍显示等待真实回执。`SELF_APPROVAL_FORBIDDEN`是明确业务拒绝，会话保留以展示原因；其他身份/市场失效的401/403会清理页面与附件。

运行`npm --prefix frontend test`及`npm --prefix frontend run build`检查组件行为和编译；`tests/unit/test_staff_entry_route.py`使用临时dist验证/staff和未知API404、无token拒绝。预期包含真实登录/退出、旧重试不借新身份、409保持输入、背景刷新不静默重基、晚编辑保存后仍需再保存才能送审。jsdom结果不证明真实320px布局、键盘可用性或跨仓旅程；这些保留在后续验收清单，不预先勾选。


## 评测安全入口交付范围

默认eval显式离线Settings、新临时SQLite和确定性依赖，不读取应用.env或继承既有业务目标；generation标未测模型质量，e2e仅离线图plumbing。真实评测需显式允许、Provider/model及cases/calls上限；调用上限不是费用保证。报告记录实际任务覆盖、源码候选digest、数据/知识与prompt版本，旧关键词工具指标明确为legacy proxy。初始化失败/取消关闭已取得资源，关闭失败日志仅记录资源/异常类，不带异常正文或URL。使用与边界以[评测指南](../../../10-evaluation.md)为准；本批不实现完整业务工具轨迹或保留集。

## 隔离部署交付实施顺序（计划）

业务问题是维护者能否从干净源码启动可核对版本的API/Worker，并在任务中断后恢复。直接沿用旧SQLite单API Compose缺少迁移/Worker；新增通用部署框架维护面过大。选择复用Dockerfile/Compose，另设明确命名的本地售后演示配置，不改变既有启动入口的语义。

第一步仅交付DTC部署基础：镜像按uv.lock安装，包含Alembic配置/迁移；独立PostgreSQL16健康后显式一次性迁移，API与Worker依赖迁移成功。绑定主机端口仅loopback、默认不读取.env/应用密钥、无云服务/生产写入；标注Fake Connector只能验证启动，不能当跨进程共享外部业务存储。凭据不写镜像或输出配置。构建上下文排除.env及变体、.work、数据、报告和凭据目录；输出镜像版本/迁移head，健康仅声明实测范围。

第二步在两仓各自新建合成数据库，以真实E-pig HTTP连接完成演示；不得用DTC/E-pig共享业务表，也不得把两个进程各自Fake当成同一外部系统。第三步针对所属新库做备份恢复到另一新目标，先停止Worker并核对原key/原批准/源回执再恢复执行；备份早于审批造成的历史缺失必须转人工，不补造审批。备份不是两仓分布式原子快照，不承诺零数据丢失。

本章验收逐项提供真实构建、迁移、健康、Worker一次轮询、隔离与清理、备份恢复和跨仓故事证据；未完成部分保留计划。继续使用现有框架，不引入队列、Kubernetes、心跳平台或任意命令/数据库目标执行器。

工具评测下一步采用eval内的窄代理：包装本次自有container.registry中的真实Tool.run，保留原工具schema/risk/parse与异常，记录实际工具名、Schema规范化参数、次数与成功/失败；不改生产Graph/Registry，不以回答关键词推断工具执行。只处理合成离线数据，报告不保存context token/完整结果/堆栈。零调用如实为零，明确区分agent_tool_calls与业务Connector调用；staff模式禁写产生的零调用不能冒充正向Function Calling质量。代理在图执行前安装，仅服务本次容器并在结束关闭；开发集期望与实际可比但真实模型质量仍未测。

## 两仓Compose运行时收尾（已实现、空feed已验证）

原生双服务HTTP已通过八故事，但DTC部署基础仍用Fake，因此还需真实Connector在容器拓扑中的验证。比较：继续原生双进程最简单但不能证明Compose；本地引入TLS代理/证书运维增加无关维护；选择仅用于合成演示的共享网络命名空间——DTC API/Worker通过`network_mode: service:epig`共享E-pig网络空间，用`http://127.0.0.1:3000`满足现有非生产loopback限制，不修改Connector或生产HTTPS要求。E-pig服务只将两个应用端口发布到主机127.0.0.1，两套PG各自命名、凭据和卷独立，均不发布数据库端口。该拓扑不作为生产扩容设计。

新增专用Compose及窄准备工具：从明确提交归档两仓受控源码，不读工作区.env；生成0700目录/0600合成环境文件、来源digest与随机项目标识；只为本项目固定服务准备上下文，不做任意命令/数据库目标平台。E-pig复用既有Dockerfile的build阶段跑database:migrate:all、runner阶段跑standalone；两边均显式迁移成功才启动。仅在受控Dockerfile副本注入相同官方基础镜像参数并补锁定workspace依赖的复制，不改E-pig业务源码或Cookie/Origin规则，不烘焙密码/token。

先验收空库实际E-pig认证feed、DTC真实EpigConnector轮询/游标与两仓迁移/健康、非授权拒绝、版本及清理；业务八故事与页面仍各自保持独立证据。准备工具不初始化任意已有PG，清理仅接受本次manifest与相符归属标签，不全局prune。完整演示的员工和订单初始化/页面流程仍需独立完成，不能把空feed部署验证当作业务闭环。

## 保留集执行协议（已实现，先冻结后执行）

12例按三层执行，不合并模型和工程成功率。6条政策图通过真实ChatService/Graph、受信任合成员工身份与对应市场/类别运行，采集实际检索引用、生成/复核/重写状态与Tool.run；规则转人工单列，不能因模型做过意图识别就称回答由模型生成。2条建议通过真实员工API保存editing或安全拒绝，核对引用/格式/零执行；4条工程守卫通过真实API/执行服务，H12必须真实API进程重启，不能用对象重建冒充。

执行器仅使用固定本地合成fixture和新临时SQLite，不接收任意业务库URL；不改生产prompt/知识/规则。默认不调用模型，模型例未跑标not_run；显式真实模式才读取专用DTC_EVAL进程环境，设置全局cases/calls/output边界，正常和异常均清理所属进程/库。报告保留原始合成答案、真实引用与阶段状态供Codex人工按事实正确/依据充分/可执行性分别0/1/2裁决；机器格式与权限检查不冒充人工评分。同Provider Faithfulness是自动证据，不等于独立人工真值。

真实模型用户已授权，不设累计费用上限。本轮选择官方deepseek-flash与显式thinking disabled；可复算报告记录实际返回model/usage、时间、源码候选/数据/知识/提示词digest。协议适配只变两个app文件，保留集首次执行前revision2已重冻结并保留旧hash；尚无保留集反馈用于调参。SDK短暂DNS失败和一次合成JSON探针格式观察分别保留，不把网络/接口探针当质量成绩。

## CLI环境的页面验收执行方式（计划）

已核实本机Browser/Chrome/Computer Use插件安装启用，但页面接口仍报timeoutMs；官方Browser文档明确内置Browser不支持Codex CLI，扩展说明面向ChatGPT桌面App。此前让用户反复重新连接不能证明问题归因，不再要求用户学习或重复配置Computer Use。

项目验收采用独立软件E2E测试：新增最小Playwright开发依赖、固定场景脚本和专用fixture启动入口，在新建隔离Chromium及合成数据库上访问本地构建页面。它不操作现有浏览器窗口、账户或profile，不修复/替代桌面插件通道。复用E-pig已有专用browser sandbox与DTC专用PG创建/迁移逻辑，不新增业务API或通用测试平台。

验收组为：消费者合法附件/申请→客服材料/草稿→另一审批人→Worker→唯一回复；消费者未知结果刷新后同key/body恢复；员工409输入保持/显式重读、退出和角色隔离；320px与桌面布局、关键键盘操作及错误/加载状态。过程必须实际点击/填写页面，不能用后台API替代关键UI步骤；fixture初始化和事后计数核对分别记。AI不可用手工处理在本组验证，真实模型质量由独立保留集负责。

输出仅合成页面截图、实际断言、状态/唯一计数、源码/依赖/浏览器版本和清理证明。截图由Codex人工查看，不把DOM通过当视觉通过；自动化浏览器验收也不冒称真实用户试用。桌面插件通道保持未验证状态，软件E2E结果独立报告。

夹具协议revision2修复H07/H08商品/材料不一致，保留旧真实报告；203个app/knowledge与原问题未调整。修订后真实执行仍暴露金额单位、材料清单和转人工提示缺口，具体裁决只在流程记录维护，不以注入守卫通过替代质量验收。
