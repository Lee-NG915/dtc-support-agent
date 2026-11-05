# E-pig 内部演示政策包：验证记录

验证日期：2026-11-05

状态：**E-pig synthetic demo 政策包、品牌运行时与本轮 Review 修复已完成。固定离线集和最新 DeepSeek Flash 生成节点集均为 34/34，前一轮 33/34、1 fallback 保留为波动证据。8003 已重启加载最终代码并复验；8002 旧进程未重启，不能视为已在原端口完成切换。**

对应实现见 [E-pig 政策包与市场隔离检索](../../architecture/modules/after-sales/epig-policy.md)，跟踪 Issue 为 DTC #3。

## 1. 政策、检索与静态门禁

| 检查 | 实际结果 |
|---|---|
| `.venv/bin/python scripts/check_epig_policy.py` | 通过：18 篇 AU/US/SG 文档，18 篇含查询感知人工复核配置；全部为内部 demo，无外部 URL |
| 完整 `knowledge` ingestion 冒烟 | 通过：202 个 chunk，其中 E-pig 75 个 chunk、18 个文档、每市场 25 个 chunk，来源均为 `internal_demo` |
| AU/US/SG 床垫保修 BM25 scope 冒烟 | 通过：分别命中对应市场的 E-pig warranty 文档，内部 source label 非空且 `source_urls=[]` |
| `.venv/bin/python scripts/check_epig_after_sales_contract.py` | 通过：售后 v1 OpenAPI、49 个 fixtures、ID、header、写入约束和 checksum |
| `.venv/bin/python scripts/check_project_docs.py` | 通过：30 个 managed Markdown 文件、9 个 development skills；只做离线结构检查 |
| Ruff format/check | 通过 |
| `git diff --check` | 通过 |
| `.venv/bin/python -m mypy` | **通过：125 个源文件**；配置范围为 `app`、`eval`，原 `deepseek.py` 的 `int(object)` 错误已通过类型收窄修复，未禁用检查 |

上述检索冒烟使用 Hash embedding 和本地内存实现，不是生产 Qdrant 验收。测试脚本中一次误将同步 BM25 `search()` 当作 async 调用导致 `TypeError`，改正调用后重跑通过；该错误不属于应用实现缺陷。

## 2. 测试与固定离线评测

### 政策迁移基线（Review 修复前）

| 检查 | 实际结果 |
|---|---|
| E-pig 首轮聚焦测试 | 54 passed / 5 failed；暴露按检索分数强制复核的误拦截和 Offline 引用文档偏移 |
| 修复后聚焦测试 | 59 passed；后续新增 Faithfulness/scope 回归也分别通过 |
| `.venv/bin/python -m pytest -q --cov=app --cov-report=term-missing` | **237 passed，总覆盖率 79%**；1 条 LangChain deprecation warning |
| `.venv/bin/python -m eval.runner --task epig-policy` | **34/34**；scope、source、document、first-document、term、review failures 均为 0 |

复核门控最初只要 review 段分数最高，即使结构化规则未命中也转人工，造成正常退款和配送误拦截。修复后仅由复合主题规则触发；没有结构化规则的旧 review chunk 仍保持 fail-closed。过滤无关 review 段后，优先保留同一首选文档的普通段，并将过滤后的 chunks 写回图状态，避免 Faithfulness 重新读入已剔除证据。过滤后没有普通证据时确定性拒答，不以 `refused=false` 空调用模型。

Faithfulness 改写、checker 不可用和最终不支持拒答均由代码恢复 E-pig demo 声明；送给 checker 的实体答案会先移除该确定性声明，避免把声明本身误判为无证据模型陈述。stateless 政策回合不再写入通用聊天历史，避免同一 session 的后续 upstream 请求读到 E-pig 回答；既有历史数据未做删除或迁移。新增回归还覆盖直接检索日期默认、原始确认 scope 保留、有效期倒置拒绝、去重保留日期/复核语义，以及答案同义词组不覆盖掉其他必要词。

### Codex Review 修复后的最终门禁

| 检查 | 实际结果 |
|---|---|
| 政策/复核/Faithfulness/契约聚焦测试 | **52 passed**；契约测试 **13 passed**，评测评分测试 **4 passed** |
| `.venv/bin/python -m pytest --cov=app --cov-report=term-missing` | **242 passed，1 warning，总覆盖率 80%**（4300 statements，880 missed） |
| `.venv/bin/python -m mypy` | **Success: no issues found in 125 source files**；仅声明 `pyproject.toml` 配置的 `app`、`eval` 范围 |
| Ruff format/check、政策检查、契约检查、文档检查、两仓 `git diff --check` | 通过 |
| `.venv/bin/python -m eval.runner --task epig-policy` | **34/34**；报告：`eval/reports/epig-policy_20261105T070129Z.json` |

修复过程曾使离线集降为 **32/34**：直接按语料顺序前置复核段改变了首选文档。随后抽出 `merge_policy_review_chunks()`，让已召回复核段保持原相对排名、仅补回被 `top_k` 截断的命中段，图、知识工具和评测共用该函数后恢复 34/34。该失败未从记录中删除。

补充说明：额外把 `tests/` 和契约脚本临时纳入 mypy 的实验性命令报告了 54 个测试桩、测试数据及脚本类型错误；这些路径不在项目 `[tool.mypy]` 与 CI 门禁范围内，本轮没有为消除非门禁错误而删测试、加忽略或扩大任务。上述“通过”严格指配置范围，不表示所有测试文件已类型检查。

## 3. DeepSeek Flash 真实生成节点评测

命令：

```bash
.venv/bin/python -m eval.runner --task epig-policy --llm
```

该评测直接调用 `generate_node`，使用固定 scope 检索证据，**不是完整 LangGraph 固定评测，也不测试 Faithfulness**。

| 顺序 | 通过数 | 模型调用 | fallback | 说明 |
|---|---:|---:|---:|---|
| 第一次 | 32/34 | 22 | 1 | 1 条模型/解析回退；“尚未出库”未命中当时仅接受“未发货”的词组 |
| 第二次 | 33/34 | 22 | 1 | 另 1 条正常回答发生回退，说明模型输出/调用仍有波动 |
| 迁移阶段最终 | 34/34 | 22 | 0 | 报告：`eval/reports/epig-policy_20261105T055022Z.json` |
| Review 修复后第一轮 | 33/34 | 22 | 1 | `au-return-materials-no-review` 失败；报告：`eval/reports/epig-policy_20261105T070259Z.json` |
| Review 修复后第二轮 | **34/34** | **22** | **0** | 报告：`eval/reports/epig-policy_20261105T070435Z.json`；latest 已同步 |

最终结果中：12 条复核/无证据保护均为确定性处理、零模型调用；22 条正常回答各调用一次；引用品牌、市场、内部来源和空 URL 检查通过，demo 声明失败为 0。为“未发货”补充“尚未出库”等等价答案词组时，证据侧 `required_terms` 未放宽，并新增测试防止同义词配置漏检“取消”等其他必要词。保留各轮结果是为了说明单次通过不代表每次模型调用都稳定，也不代表任意自然语言问题零错误；Review 修复后的 33/34 报告同样保留。

## 4. 服务与真实 HTTP 冒烟

由于停止 8002 上既有 uvicorn 进程的本地权限请求连续两次被分类器拒绝，旧 PID 62221 仍在监听。没有通过其他方式绕过权限，也没有把旧进程当作新政策服务。

已在 `127.0.0.1:8003` 启动加载当前代码的 uvicorn 进程，并完成：

| 检查 | 实际结果 |
|---|---|
| `/api/v1/health` | HTTP 200，`status=ok` |
| `/api/v1/ready` | HTTP 200，`ready=true`；SQLite、DeepSeek 配置和内存向量可用，Redis `not_configured` |
| `/api/v1/capabilities` | HTTP 200；`database=sqlite`、`cache=memory`、`vector_store=memory`、`reranker=lexical`、`llm=deepseek`、`degraded=true` |
| AU 家具保修真实 HTTP | HTTP 200，包含 4 年、demo 声明和 AU `internal_demo` warranty 引用 |
| US 家具保修真实 HTTP | HTTP 200，包含 3 年、demo 声明和 US `internal_demo` warranty 引用 |
| SG 家具保修真实 HTTP | HTTP 200，包含 2 年、demo 声明和 SG `internal_demo` warranty 引用 |

三条 HTTP 请求经过实际图和生成后处理，但只是三市场冒烟，不替代完整图固定集、Faithfulness 固定集或故障注入。历史隔离修复后曾再次重启 8003 并复验；本轮 Codex 修复完成后又停止旧 8003 进程、启动最新代码，并复验 `/health`、`/ready`、`/capabilities` 均为 HTTP 200，AU 家具保修返回 4 年、demo 声明和 AU `internal_demo` 引用。8002 原端口重启仍是本地操作阻塞项；当前可验证的新服务地址为 8003。

## 5. Codex 双仓 Review 与剃刀裁决

### 审查方法与采纳结果

本机 Codex CLI 0.166.0 使用 `deepseek-flash`、read-only sandbox 分别审查：DTC `main...HEAD`、DTC 当前未提交改动、E-pig 当前分支与工作区。审查未读取或输出 `.env`，未执行真实外部写入。只读环境未能重跑完整 pytest/离线评测，因此 Review 报告中的旧测试数字没有被当作 Codex 复验结果；本文数字均由主会话在可写测试环境实际重跑。

主会话逐条复现和判断，没有直接照搬结论：

| Review 发现 | 裁决与实际处理 |
|---|---|
| 政策问题误标 `CHITCHAT` 可绕过检索与 Faithfulness | **采纳 P1**：只允许确定性的纯社交文本豁免；政策问题误标时强制检索或无证据拒答 |
| 定制释义漏拦截、配送问题误拦截、复核段被 `top_k` 截断 | **采纳 P1**：补复合同义词、删除宽泛“订单”条件、独立匹配当前 scope 的复核规则并保持引用排名 |
| Faithfulness 改写后保留旧引用 | **采纳 P1**：返回并校验新 `citation_chunk_ids`，改写依赖 B 时不再引用 A；extractive fallback 使用实际首证据 |
| 统一拒答缺 demo 声明 | **采纳 P2**：`refuse_node` 读取 scope 并走统一声明后处理 |
| 客户端可自报 brand/market | **保留为明确 demo 限制**：本轮不伪造生产授权；真实订单接入前必须由可信服务端上下文派生 scope |
| provider 的 `int(object)` 使 CI mypy 失败 | **采纳**：显式类型收窄，新增 reasoning token headroom 回归；配置范围 mypy 已通过 |
| Koala 历史文档含已删除命令和“无剩余阻塞”表述 | **采纳文档修正**：明确命令不可执行、旧结论仅限当时固定样例，不改写历史结果 |
| E-pig 计划漏 `/service`、设计 pending 队列、复用消费者订单路由、校验 DTC 审批版本 | **采纳计划修正**：消费者/service 路由分离，使用追加式 feed；DTC 是唯一审批权威，E-pig 只校验自身订单版本与幂等 |
| E-pig 手写第二套路由/错误/字段语义 | **采纳**：产品蓝图、对接规范、OpenAPI 各自保持唯一权威，E-pig 只写本仓模块落点和任务 |

售后契约审查采纳项、49 个 fixtures 与 checksum 证据见 [契约验证记录](epig-contract-v1.md)。

### 过度设计清单

“剃刀”用于删除没有调用方、重复权威或尚无需求支撑的抽象，不用于删除权限、隐私、幂等、并发和证据边界。

| 项目 | 判断 | 本轮处理 |
|---|---|---|
| `extra["policy_review_required"]` | 写入后无人读取，生成重新计算，属于死状态 | **已删除**；`policy_review_chunks()` 保持唯一判断入口 |
| 图、知识工具、评测各自拼接复核段 | 三个真实调用方需要同一排序语义，但不应复制实现 | **保留一个纯函数** `merge_policy_review_chunks()`；没有新建服务或框架 |
| 三市场重复 review rules、每篇相同 product categories | 数据重复有维护成本，但当前共享继承/override 会新增另一套解析和失败模式 | **标记为候选，暂不抽象**；出现真实市场差异后再以数据驱动方式收敛 |
| Koala profile、旧 dataset、旧文档 | active 已不用，属于清理候选；旧 payload 兼容与历史证据仍有用途 | **保留兼容和历史，不作为 active 政策**；历史文档已标注不可执行 |
| `approved` 状态与 `source_updated` | 当前 E-pig demo 不分流使用，存在未来维护成本 | **本轮不贸然删除**，避免破坏旧模型和正式政策边界；不新增依赖它们的分支 |
| `CONSUMER_ALLOWLIST_SCHEMAS` 名称清单 | 是第二份 DTO 分类清单，新增 Schema 时可能漏维护 | **保留显式清单并扩到安全 DTO**；不为此发明 `x-*` 扩展体系 |
| 产品 `AS-ACC-*` 与项目 A1～A12 | 两套验收目录可能漂移 | **标记重复维护风险**；跨仓联调统一引用 `AS-ACC-*`，本轮不重写既有工程验收体系 |
| 预设“80 个业务任务 + 40 个工程场景” | 基线前固定规模偏早 | **标记为规划候选**，不把数字当已完成；保留固定集、保留集和人工评分原则 |
| 九个生命周期技能 | 使用数据不足，部分规则与指南重复 | **标记低频合并候选**；权限、验证和证据边界不删除 |
| 来源哈希格式检查 | 不能证明外部内容完整性 | **已澄清证据口径**；本轮不伪造内容 hash 验证 |
| E-pig 复用消费者路由承载 service DTO | 混合 Cookie/Bearer 和脱敏/审批 DTO | **已从计划删除**，改独立 service Route Handler |
| E-pig `status=pending` 偏移队列 | 契约不存在且有漏读风险 | **已删除**，使用连续高水位 feed |
| E-pig 第二套审批版本校验 | 形成第二审批权威 | **已删除**，只保存审批审计值并校验自身 `order_version` |
| E-pig 消费者多轮回复/内部审批时间线 | 超出 v1 且可能泄露内部状态 | **不采用多轮和内部状态**；保留产品要求的真实 request/ticket 进度与唯一最终回复 |
| vendor、source-lock、fixtures、freshness | 尚无运行时实现，但对应跨仓漂移和隐私/恢复风险 | **保留为计划任务，不提前宣称实现**；不增加第二套契约 |

完整 `PolicyScope`、缓存隔离、来源字段、demo 声明、人工审批、角色/市场隔离、稳定幂等键、`(market, request_id)` 唯一约束、`order_version`、`LOOKUP_FIRST`、安全 DTO 和共享 fixtures 都有明确故障或验收依据，**不是本轮剃刀删除对象**。

## 6. 未验收范围

早期政策 Review 代理多次受模型额度/连接限制中断，主会话已根据返回线索修复复核误拦截、Offline 引用偏移、Faithfulness 证据与声明、过滤后空证据、直接检索日期、确认 scope、去重、评测词组和 stateless 历史回写问题。本轮 Codex CLI 三份 Review 已完整产出并由主会话裁决，但仍不等于独立审查者对修复后全部代码签字通过。

完整 LangGraph 固定集、Faithfulness 固定集、生产 PostgreSQL/Redis/Qdrant、真实 E-pig 联调、真实历史订单版本和生产政策批准仍未验收。所有 E-pig 条款仍是 synthetic demo，不用于真实订单裁决。本任务未执行真实外部写入，未使用真实客户数据，也未获得新的提交/推送授权；本轮按用户要求修改了 E-pig 的计划文档，但没有修改其运行时代码。
