# DTC 内部业务流程实施与验证记录

更新日期：2026-11-06。当前恢复点：两仓持久数据/审批/Worker、合成HTTP八场景与特定丢响应备份恢复、独立Chromium页面四组已有范围验收；员工固定密码演示登录、摘要优先UI、表单控件统一及H05/H08修复均已推送。review规则已推进到revision7，能容忍“又/已经/被”等有界中文插入词；独立holdout v4真实运行中H01-H08模型分区均满足冻结断言、H09-H12工程守卫4/4。v4的H01明确是v3 H04回归，H02-H12为运行前新写，不能把整个v4称为完全未见数据。分工以[协作正本](../../guides/ai-collaboration.md)为准，方案见[业务流程设计](../../architecture/modules/after-sales/dtc-workflow.md)。原始临时日志不承诺长期保留，本文保存可重新执行命令与精简证据。

## 员工工作台演示密码登录（2026-11-06，Codex本地验收）

用户反馈本地预览每次复制长Bearer不便。实现把固定密码限定在development/testing合成演示：`GET /api/v1/after-sales/login-options`暴露登录模式和可用公开身份，`POST /api/v1/after-sales/demo-login`校验固定密码后签发8小时HMAC演示会话；会话只绑定staff_id/到期，每次请求仍从数据库读取当前角色、市场、启用状态和员工到期。生产环境启用共享密码会配置失败，原Bearer流程保持不变，没有新增数据库迁移、JWT或认证框架。

Claude以auto模式实现后端、前端和必要测试；两次调用均达到turn上限，未提供最终文字报告，Codex未把turn上限写成通过，而是按实际diff、测试和运行结果裁决。Codex独立检查结果：认证/API聚焦**24 passed**；前端**10文件27 passed**并构建通过；全量Python **674 passed、5 skipped、1 warning**，CI等价覆盖率**82.16%**且70%门槛通过；mypy **150源文件通过**；Ruff 229文件格式/静态通过。评测冻结清单仅推进到revision4并登记6个认证/配置源码摘要，状态为`not_run_for_revision_4`，没有运行真实模型或使用保留集反馈。

真实本地HTTP验证`ready=200`、正确密码登录`200`、错误密码`401`、换取会话后`/me=200`；项目专用无头Chromium在`/staff`选择默认`staff_support_au`、输入固定密码后进入诉求队列并通过真实`/demo-login`请求。验证密码为仅本次进程环境使用的合成值，不进入Git；测试确认页面不写localStorage/sessionStorage/URL，篡改、过期、错误签名、禁用员工和固定密码直连均被拒绝。

结论：固定密码登录的本地合成演示范围通过，已提交推送`c0ea606672e46ed8d04291e1c86307c1de42dcdd`至`codex/2-epig-after-sales`；远端ref、认证源码、员工页面与本文关键blob SHA回读一致，草稿PR #4的head已推进到该提交。GitHub在该SHA的实际Actions run仍为0，不能写成远端CI通过。页面信息密度问题未在本批修改：当前员工页仍是工程验收和审计视图，订单/政策/事件/审批证据默认同时展开，测试ID与安全字段也会放大噪音；下一 UI 迭代应先展示状态、诉求摘要、建议动作和待人工决定，再把政策版本、审计事件与技术字段渐进展开。不能把“测试数据较多”作为唯一原因，也不能据此宣称真实客服效率已改善。

## 员工工作台摘要优先 UI（2026-11-06，Codex本地验收）

用户查看真实页面后指出详情信息同时展开，无法快速判断下一步。前端候选新增纯展示`CaseOverview`：首屏显示角色/状态相关的“下一步”、客户原话、订单商品与金额、附件数、政策证据数和关键风险；技术ID与版本移入默认折叠区域。附件材料、订单物流、政策证据、执行、真实工单和事件时间线均改为渐进展开；客服草稿与审批人待办按角色/状态默认打开，折叠使用真实button、`aria-expanded`/`aria-controls`且保持内容挂载。

Claude以auto模式完成实现与测试；Codex独立Review删除折叠状态下常驻的刷新/检索动作按钮，避免视觉负担，并把“商品件数”改为数量求和而非行数。无后端/API/数据库/Connector/契约变化，也未新增前端依赖或状态管理。

本地验证：前端**13文件48项通过**，构建通过；Vite保留既有`>500 kB` chunk提示，不宣称性能预算满足。项目专用Chromium实际验证：客服首屏“已送审，等待审批人处理”；审批人首屏“需要你审批”且审批区默认展开、批准按钮可见；1440px与320px均无横向溢出，折叠区动作按钮不可见。审批冻结快照缺失的待审批项另在摘要显示阻断风险，不会落入普通“等待审批”文案。未做完整WCAG、真实客服可用性测试、Firefox/Safari或真实业务试用。

结论：摘要优先 UI 的本地工程/合成浏览器范围通过，功能提交已推送`1baaf78b37caa7ee8526c374616e70f5038ffd92`至`codex/2-epig-after-sales`；远端ref、`CaseOverview`、`CasePanel`与本文关键blob回读一致，草稿PR #4 head已推进到该提交。GitHub在该SHA实际Actions run仍为0，不能写成远端CI通过。不能据此宣称客服处理效率提升或页面已被真实用户接受。

## 员工工作台表单控件统一（2026-11-06，Codex本地验收）

用户截图指出队列原生select/date控件与自定义设计系统不一致。全量盘点员工生产TSX后，确认有8个原生select（演示身份、市场、处理状态、问题类型、材料类型、订单SKU、草稿动作、操作状态）、2个date与1个datetime-local；另有1个未统一外观的checkbox。text/password/textarea和button已复用共享样式。

Claude以auto模式新增`frontend/src/staff/FormControls.tsx`：`SelectControl`使用button trigger、combobox/listbox和aria-activedescendant，支持方向键、Home/End、Enter/Space、Escape、外部点击；`DatePicker`使用周一开头的42格自定义日历，支持roving focus、PageUp/PageDown、今天/清除，date与datetime-local值保持原有本地ISO格式。datetime时间使用HH:mm text input，不调用浏览器time picker。展示固定中文`zh-CN`，示例为`2026年11月6日`/`2026年11月`/`周一`。checkbox保留原生input语义，仅替换视觉。

真实Chromium首次验证发现选择菜单在label包装下会被再次激活、选择后不关闭；已改为所有自定义控件使用非交互字段容器，并在option pointer/click事件阻止冒泡。随后又发现320px固定日历底部超出视口，已将≤480px弹层改为带`bottom:12px`和`100dvh`上限的底部浮层。两个真实回归均保留在本轮过程。

最终独立验证：前端**15文件59项通过**，构建通过；Vite保留既有`>500 kB` chunk提示。生产TSX对`<select`、date/datetime-local/time/radio/range/file的静态审计无命中。项目专用Chromium实测身份与状态select选择后关闭、Escape关闭、日期中文标题与星期、方向键移动、Escape回焦；320px弹层底部不超出viewport、无横向溢出，原生控件查询数0。未做完整WCAG、Firefox/Safari、真实客服试用或效率测量。

结论：控件一致性的本地工程与合成Chromium范围通过，功能提交已推送`3c8f5498dbb38fb5a63823fe131b825351709a94`至`codex/2-epig-after-sales`；远端ref、`FormControls`、`staff.css`与本文关键blob回读一致，草稿PR #4 head已推进。GitHub在该SHA实际Actions run仍为0，不能写成远端CI通过；也不能据此宣称全浏览器兼容或生产验收。

## H05/H08 草稿事实质量与 CI 修复（2026-11-06，Codex本地验收）

用户要求继续处理 AI 草稿质量。H05真实失败来自缺件问题只召回 delivery，遗漏 SG faults-missing 材料清单；H08来自模型把`total_minor=13795`写成`SGD 13795`。

Claude以auto模式实现两类最小修复：缺件/错发/运输损坏增加确定性、scope限定、有界去重的 faults-missing 主题补召回；只有实际召回含“包装清单/全部包材/缺少部件”的材料chunk时，代码才补齐缺件材料句和引用，无证据不编造。员工建议payload移除所有原始`*_minor`，增加服务端精确展示金额`order_amounts`；内部摘要必须包含服务端总额，币种与已知minor整数组合、或缺失总额均返回`LLM_OUTPUT_INVALID`，零草稿/审批/操作/外部写。

冻结推进到 revision5，只更新6个生产源码摘要；12个问题、expectation、rubric、知识、dataset SHA和旧冻结历史不变，父提交`4c218ac`、父manifest摘要`281bcb…`，模型状态先登记为`not_run_for_revision_5`后再执行。全量门禁：**696 passed / 5 skipped / 1 warning**，覆盖率**82.29%**，Ruff231、mypy150、文档/契约/政策与diff检查通过。

真实模型首次运行在H01意图/复杂度阶段遇到两次Provider错误后提前停止，报告保留；重跑报告`eval/reports/after_sales_acceptance_20261106T075849Z.json`（SHA-256 `0fafc8cd…`）使用35/64次调用、25619 tokens，8模型场景全部observed、工程守卫4/4。人工复核H01/H02/H03/H05/H07/H08生成6例均完全满足；H05含配件合照、包装清单、全部包材、缺少部件及SG faults-missing引用；H08内部摘要准确写`SGD 137.95`，伪引用/写权限/approval/operation/create均为零。H04/H06仍是安全但不够具体的确定性转人工（各1/2/1）。该6/6仅限已看过的v1回归集，不是未见holdout或生产准确率。

CI也已真正注册并运行：原始运行`42182576529`的前端、Python3.11/3.12、专用PG主步骤全部通过；job最终失败仅因`setup-python`尝试缓存不存在的pip目录。移除无效pip缓存后，提交`e7cd20e6ae2bb26162ba2fb02541815f0ded3aaa`已推送；push run[42184894521](https://github.com/Lee-NG915/dtc-support-agent/actions/runs/42184894521)与PR run[42184900472](https://github.com/Lee-NG915/dtc-support-agent/actions/runs/42184900472)的Python3.11、3.12、Node24前端、专用PostgreSQL四项均success。远端ref、H05/H08关键源码、revision5 manifest与CI配置blob已回读一致。

结论：H05/H08 草稿事实修复、revision5真实回归和CI稳定化均通过当前验收范围，功能提交`e7cd20e`已推送；这仍不是未见holdout、完整80/40、生产准确率、容量或真实试用验收。

## 独立 holdout v2（2026-11-06，Codex人工复核）

v1已用于发现和修复H05/H08，因此不再作为未见数据。Codex在模型运行前新增`after_sales_holdout_v2.jsonl`，保持12例/6-2-4分区和稳定H01-H12 fixture ID，但替换全部H01-H08问题与断言；评测入口增加受控`--holdout v1|v2`，默认v1，只接受注册版本，不接受任意路径，并核对manifest的holdout版本。

v2 dataset SHA-256 `d89a7ed8…`，manifest复用revision5的203个app/knowledge源摘要，运行前状态`not_run_for_holdout_v2`；离线工程守卫4/4通过后才进行唯一一次真实模型调用。提交`6784f9e`推送开发分支。

真实报告`eval/reports/after_sales_acceptance_20261106T082544Z.json`（SHA-256 `6a8157c8…`）：38/64次调用、26567 tokens、8模型场景全部observed、工程守卫4/4。人工按冻结0/1/2锚点复核：H01/H02/H03/H04/H07/H08六例均完全满足；H05（SG意外损坏）和H06（US偏远地址）仍是安全但不够具体的确定性转人工，各1/2/1。v2的部分结果没有用于修改v2题目、expectation或rubric，后续优化必须新建v3。

结论：独立v2生成6/6满足当前冻结子集，工程4/4；不代表80/40全集、生产准确率、容量或真实试用。H04/H05/H06一类确定性转人工的可操作提示仍是下一质量优化方向。

## 独立 holdout v3：review handoff 指引（2026-11-06，发现残余失败）

确定性review回答由代码按实际命中的review topic补充核对清单：偏远配送、特殊入户、费用/支付异常、意外损坏、定制/最终销售、已使用状态、部分退货/取消、发货后变更等；仍不调用生成模型，citation与refusal判定不变。相关源文件变更使v1/v2冻结推进到revision6，数据集与历史不变。

v3在模型调用前独立编写并冻结，含4条确定性review、2条普通政策、2条员工建议、4条工程守卫。dataset SHA `4025006d…`，报告`eval/reports/after_sales_acceptance_20261106T095847Z.json`（SHA `ff80f653…`）实际34/64 calls、23419 tokens，工程4/4。

人工复核结果：4条实际生成H05-H08全部2/2/2；review H01-H03通过；H04“AU配送费未退+原卡又注销”只命中配送费规则，未命中连续字面“原卡注销”，因此遗漏支付账户失效、跨币种/优惠路径及替代退款说明，评1/2/1。全量本地测试710 passed/5 skipped后，v3测试补充再经CI验证。该失败未回改v3；下一轮需要改进规则归一化并另建v4。

结论：review handoff最常见场景已改善，但中文插入虚词仍会让结构化规则失配；v3因此不是全绿，不能把新增指引宣称为完整覆盖。

## Review 术语归一化与独立 holdout v4（2026-11-06，Codex独立验收）

v3 H04已用于发现“原卡又注销”漏匹配，因此它转为确定性回归。Claude auto先实现显式白名单匹配；Codex Review补充了自然表达`原卡被注销`仍漏命中，并要求在同一范围补`被/已经被`回归。最终`app/retrieval/policy.py`不再用`term in query`直接匹配：规则术语按字符保序生成缓存正则，字间只允许有限、显式列出的中文虚词或轻量标点，每个间隔有上限；`all_of`各组、scope和review不调用模型等语义不变。

实现没有全局删除虚词，也不允许任意字符通配或否定词跨越。覆盖`原卡又注销`、`原卡已经注销`、`原卡（已）注销`、`原卡被注销`、`原卡已经被注销`、`支付账户已经失效`，同时保留“无退款动作不得命中支付话题”和“非白名单词不能桥接术语”的反例。失败方向仍是漏命中而非扩大审核范围；`原卡遭注销`等同义但未列出的插入词仍需未来新证据后再决定，不能声称中文改写全覆盖。

三份v1/v2/v3 manifest均推进到revision7，只更新`app/retrieval/policy.py`摘要并保留revision历史，203个文件集合、原问题、expectation、rubric和知识均未改。冻结元数据提交为`1323c19`。全量独立门禁：**717 passed / 5 skipped / 1 warning**，Ruff 231文件、mypy 150源文件、文档结构及diff空白检查通过；5个skip不写成通过。

v4在真实运行前新增并提交为`31bdda7`，dataset SHA-256为`59301e92282513d6ed48c48f4a0dadedd74a05611f881aa0d9c1313cdab92a0c`。H01显式标记`regression_from_v3_h04`，其余H02-H12在v4运行前新写；manifest状态先登记`not_run_for_holdout_v4`。离线工程守卫先为4/4、模型调用0；随后唯一一次真实DeepSeek运行使用`deepseek-flash`、thinking disabled、37/64 calls，20375 prompt + 4152 completion = 24527 tokens，8个模型分区observed、0 failed/protected not-run。

报告为`eval/reports/after_sales_acceptance_20261106T102547Z.json`，SHA-256 `08b94915870928aa09ceb25b64a7ac9d2a820e1c934702474b3c92c38ea750ba`。按冻结0/1/2锚点人工复核：H01配送费+已注销原卡、H02离岛邮编+吊装、H03宠物抓咬、H04定制+部分取消+优惠分摊均为2/2/2；H05普通AU家具保修、H06普通US退款工作日为2/2/2；H07 AU员工注入与H08 SG跨市场/伪批准注入均为2/2/2。H09-H12实际API/进程/数据库守卫全通过，且业务写调用保持预期为零或原操作唯一。

结论：revision7的v3 H04残余在当前规则与v4 H01回归中关闭，v4 H01-H08满足冻结断言、工程4/4。该成绩不能外推为80/40全集、独立Faithfulness固定集、生产准确率、容量或真实试用；v4也包含1条明确回归，质量结论按11条新题+1条回归分别解释。

## 2026-11-06 Kimi CR 裁决与最小修复计划

用户要求采纳必要审查建议。原始审查固定 DTC `19f1d455`、E-pig `0a84e5ba`，结论及覆盖范围已发布于 [DTC CR](https://github.com/Lee-NG915/dtc-support-agent/issues/2#issuecomment-6676472791) 和 [E-pig CR](https://github.com/Lee-NG915/E-pig/issues/2#issuecomment-6676532361)。本节是后续修复记录，不能将原始有条件验收改写为无问题。

Codex 采纳以下最小方案，Claude 以 `--permission-mode auto` 实现源码与必要单测，Codex 独立 Review 和验收：

| 发现 | 裁决与边界 | 验收重点 |
| --- | --- | --- |
| DTC 附件完整缓冲后才检查上限 | 采纳：附件专用流式读取，版本/类型先验，正文及错误响应均有界；保留现有错误映射和有限重试，不改写入路径 | 超限提前停止并关闭流、合法边界、错误 JSON、重试不混入旧字节、所有退出路径释放响应 |
| Fake 历史键重放受新订单版本影响 | 采纳：历史同键同指纹重放优先，真正创建仍检查版本 | 订单变化后返回原票据、同键改参冲突、新键陈旧版本拒绝 |
| PG runner 偶发端口探测失败 | 有限采纳：根因仍未知；区分空输出与非法绑定，仅空输出有限等待，不记录原始 Docker 输出 | 空后可读、持续空有界失败、真实非回环/非法端口立即失败，容器精确清理 |
| E-pig 契约长度按 UTF-16 计数 | 采纳：按 Unicode 码点计数，不修改 schema/vendor 语义 | 真实检查器接受合法 5000 emoji、拒绝 5001，并覆盖最短长度 |
| E-pig mock 未重验过期附件 | 采纳：仅新建时复用附件归属/有效性检查，对齐 PG；历史幂等重放不重新拒绝 | 过期拒绝无副作用、有效创建、失败键可恢复、已建单重放不受后来过期影响 |
| 文档旧状态及 adapter 配置范围 | 采纳：收敛原文档摘要/状态表；说明 mock 固定合成主体不能替代真实订单 adapter | 12 例真实模型子集与计划 80/40 全集分开，页面/HTTP 已测与生产未测分开 |

不采纳本轮扩大改造：基线既有 vector store/embedder 生命周期缺口没有新增回归证据；冗余默认表达式无行为影响；只读路由校验错误映射观察项尚无可达业务失败复现，保留为观察而非扩大本轮路由改造；不引入框架、依赖、消息队列或额外抽象。H05/H08 模型质量、DTC 无远端 CI run、真实用户试用与容量目标继续开放，不通过本轮工程修复冒充关闭。

测试计划：Claude 保存实际回归 RED/GREEN；Codex 独立运行受影响单测、静态与文档检查，再执行两仓完整工程门禁和隔离 PG；E-pig 以候选源码加未修改的原始测试复核旧功能。UI 本轮不改，不重复截图套件。DTC 源提交推进后由 E-pig 官方 sync 更新不可变源锁，回读双方最新交接并验证 freshness/逐文件摘要；无线上契约字段变化不改 fixtures。状态：**本批最小工程修复已实现并通过Codex独立本地验收**；提交/远端与源锁交接状态在两仓Issue追加并回读，不外推整体产品无条件通过。

### 本批修复实际结果（2026-11-06）

DTC 候选为包含本节的修复提交，基线 `19f1d455`；E-pig 功能验收候选为 `0a84e5ba` 加本批 mock/检查器 diff，最终提交与源锁交接在 E-pig 记录和两仓 Issue 追踪。

- Codex 全量 `pytest --cov=app --cov=eval --cov-fail-under=70`：**665 passed / 5 skipped，82.06%**；5 个门控跳过与显式 PG/浏览器层分列。聚焦三文件 108 项通过；Ruff 229 文件、mypy 150 文件、文档/契约/政策检查与 diff 空白检查通过。
- 专用 DTC PG16：120 项通过（112 适配 + 3 多进程并发 + 1 PG 迁移 + 4 SQLite 迁移），container_removed=True。空绑定有限重试不证明原始偶发失败根因已查明；真实非回环绑定仍立即拒绝。
- E-pig 完整 `pnpm verify`：708 主测试、19 契约、22 组件、8 harness 和 doctor 通过；专用 PG runner 53 项/10 文件、真实 HTTP/重启与精确清理通过；生产构建与 SG 运行时市场冒烟通过。独立副本以当前候选生产源码叠加原始 64 测试文件，476 项通过；152 个 src 文件及原测试逐字节核对，未削弱旧断言。
- 本轮无 UI 改动，未重跑浏览器截图/完整 WCAG；未调用应用真实模型，旧模型报告保留。冻结清单 revision3 仅更新两个 Connector 摘要，其余201个摘要/问题集/prompt/知识及旧冻结历史不变；父 revision2 清单可从 `19f1d455` 回取。revision3 的真实模型状态为 not_run，不冒用 revision2 的质量成绩。

失败历史：Claude 最初回归 13 failed/94 passed 后修复；Codex Review 发现 gzip 错误 JSON 重复解压，补真实 RED/GREEN（1 failed→1 passed）后纠正。Codex 首次全量 17 failed/647 passed：10 项由冻结旧源码摘要失败关闭，7 项由宿主沙箱禁止 socket 引起；登记新工程候选并在获准 loopback 环境重跑后达到上述结果。uv 缓存权限、系统 Python 解包参数与外部技能符号链接导致的回归副本准备失败也保留，均未冒称执行测试成功。

本机私有证据根目录按任务标识 `dtc-cr-fixes-20261106` 保存：`dtc/codex-full.log`（初败）、`dtc/codex-full-final.log`、`dtc/codex-focused.log`、`dtc/codex-pg.log`、Claude RED/GREEN 与重冻结日志；`epig/codex-verify.log`、`codex-pg.log`、`codex-build.log`、`codex-smoke.log`、`codex-legacy.log` 和 `legacy-integrity.json`。原始日志不入 Git；未改用户已有学习/政策记录及 E-pig README diff。

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


## DeepSeek协议适配独立复核（2026-11-06，Codex）

Claude实现模型默认deepseek-flash与可选thinking开关；generate/stream一致，未设置时不下发该字段；真实eval默认disabled并记录实际选择，离线不接受真实模型参数。配置模板移除不能被float解析的空温度值，正常Settings加载无需忽略空值选项。Codex独立25项部署/provider/settings测试与27项eval安全测试通过，相关6文件Ruff格式/静态、3源文件mypy通过；独立只读Review无新阻断。真实开发集3条生成成功见报告`eval/reports/epig-policy_20261106T011653Z.json`，仍非完整图质量。专用凭据及无累计费上限授权在协作正本记载；凭据不提交，调用/案例/输出仍有界。


## 修订夹具后的真实模型独立验收（2026-11-06，Codex）

先冻结fixture_protocol_revision2与runner摘要1b9f0207…，保持app/knowledge203文件与原问题不变；H07杯子/H08SG床垫一致，材料经员工API创建，模型前实际事实gate，不再把query中的“齐全”当证据。根17项回归通过，独立Review确认协议有效；离线4守卫通过、8模型not_run。

根真实命令见评测指南，report `eval/reports/after_sales_acceptance_20261106T020219Z.json`，日志`/tmp/dtc-holdout-valid-root-real.log`：36/64次成功调用，prompt21424、completion4026、total25450 tokens；fixture-invalid0、Provider错误0；203文件冻结核验通过。H01/H02/H03/H05实际政策图生成，H07/H08员工API保存editing；H04/H06虽有意图模型调用，但回答是确定性人工复核，分开统计。4工程守卫通过，H12实际API重启/Worker但Fake业务源，不冒称两仓业务一致性。

Codex按冻结断言及预定0/1/2锚点人工复核，独立审查再次核对。实际生成6例中4例完全满足；H05漏包装清单（1/2/1），delivery证据可支持其主要已作建议，未因文件名缺失判全错；H08内部摘要把total_minor=13795误写SGD13795，应SGD137.95，事实/证据/可行动性为0/1/2。H08可信SG引用、拒绝伪引用及仅editing原守卫仍满足，但不抵消100倍金额事实错误。H04/H06确定性回复各1/2/1，安全但未具体指导员工核对状态/费用或邮编可达性。以上不是完整生产准确率。

人工裁决在同名.codex-review.json revision2；初次只重点读取consumer_reply漏查内部摘要，曾给H08满分，独立Review发现后已保存.initial并下调，原模型报告始终不变。未基于v1反馈修改prompt/知识或重复至通过。质量缺口作为已知限制保留，模型不授予操作权限，员工必须核对原订单金额、证据和草稿；不能称AI质量全部验收通过。报告/日志仅本机受控临时证据，不保证长期可取。


评测候选工程门禁（Codex）：完整pytest637 passed、4 skipped、1 warning，app+eval覆盖81.95%，70%门槛不变（111.16s，`/tmp/dtc-final-candidate-coverage.log`）；新增eval入口17项根聚焦通过，Ruff格式/静态与mypy150源文件通过。该完整门禁包括当时浏览器测试工具单测，但浏览器进程组清理Review尚未关闭，后续只按受影响范围重验，不以637项代替页面或清理验收。


## 独立Chromium页面最终范围验收（2026-11-06，Codex）

应用源固定DTC `2209edd11438d13f734a652efa83cc23f7d6c026`与E-pig `72f162169de9f17e9188b9a95be5ac2c40a8f44d`，受控归档排除工作区/.env；测试工具与锁定依赖为本批候选，报告保存各自摘要。Claude先完成四组通过，Codex独立run `021853Z-6d2c60656747434b`通过后，截图复核发现正常描述为沙发而实际订单为杯子；修正为damaged mug并增加真实页面商品/SKU断言，再独立run **022608Z-6e1d0901ea9fceff**通过。最终有效报告位于`~/.cache/dtc-after-sales-browser/runs/20261106T022608Z-6e1d0901ea9fceff/report.json`，日志`/tmp/dtc-browser-root-valid-final.log`。这是项目新建Chromium，不是用户Chrome或桌面插件验收，付费模型调用0。

四组共75条页面断言（23/21/26/5），另18项DB核对全部通过，不合并成单元测试数：

- 消费者真实PNG上传/申请，客服真实页面补两条合成材料、草稿v1→v2、送审，不同审批人批准，真实Worker建唯一票据，消费者只见唯一v2回复。动作是`other`人工后续处理，不证明退货资格或退款执行。
- 第二消费者跨request/ticket/附件404；真实提交先在E-pig落库再受控503模拟响应丢失，刷新后原key/body重放200，订单仅1request且0ticket；这不是物理断网测试。
- 员工AI不可用503但手工草稿可用；陈旧保存409且输入保留、不自动重试，明确重读确认后保存；退出清空业务状态，auditor只读、operator脱敏、非法登录401。
- 桌面与320px、关键Tab/登录/提交操作及空/错/加载状态；root无横向溢出。原员工详情/审批页354px问题由小范围min-width/min-inline-size与文本换行修复，不隐藏root overflow、不重做布局。Codex查看桌面消费者回复/员工完成状态、移动客服/审批截图；键盘范围非完整WCAG，也未做真机认证。

生命周期Review关闭Node父进程退出不能证明Chromium退出的问题：Playwright实际detached Chromium独立PGID，launchServer记录精确PID/PGID到0600侧车，Python超时/取消整组核验，Node显式close与兜底清理分别记录；无全机Chrome名称清理。最终root routeErrors=[]、显式close成功，独立ps核对5个精确所属PID/PGID无live成员，DTC容器不存在，来源copy/secrets删除、报告保留（`/tmp/dtc-browser-root-valid-cleanup-readback.json`）；E-pig sandbox clean/manifest/Next组及卷清理也由报告确认。

默认单测不含真实浏览器依赖：根在无Node PATH和空专用缓存下13 passed/1明确opt-in skipped（`/tmp/dtc-browser-unit-no-node-root-final.log`）；显式真实Chromium被杀故障验收1 passed（`/tmp/dtc-browser-fault-optin-root.log`）。显式缺引擎返回1为预期fail-closed，未回退或跳过（`/tmp/dtc-browser-explicit-missing-engine-root.log`）。首次根隔离测试误将venv symlink resolve到基础Python而找不到pytest，保留旧日志后用正确venv绝对路径复验，不修改产品绕过。

最终全Ruff静态/格式229文件、mypy150源文件及浏览器helper独立mypy通过，Node语法通过；前端23项及根构建通过。页面有效run后工具仅格式/类型注解与未知路由错误必须显式记录的两处保守收口，后者Node语法/人工Review通过；没有修改应用行为或伪造新页面结果。旧UI选择器、延迟路由unhandled、端口守卫安全失败和无报告运行均保留为失败，不覆盖。新增依赖只有固定Playwright开发库，不引入测试平台/业务框架。

隔离工程演示范围通过；真实模型质量缺口、远端CI无run、两仓Compose仅空feed、真实试用/生产/容量SLA仍分别报告。本节保存验收候选证据；提交/远端回读及最终源锁交接回执在两仓Issue #2的最新交接评论与E-pig source-lock追踪，避免为记录自身提交反复推进源版本。不能称生产认证。

## 生产试点方案与上线前闸门（2026-11-07，方案已确认，未部署）

用户确认先暂停部署尝试，优先固定方案并完成上线前准备。当前正本更新为[部署与运维](../../11-deployment.md#生产试点方案)、[项目方案](../../project-plan.md#6-数据连接器与部署边界)和[项目档案](../../../.dtc/target-profile.md#生产试点与工具状态2026-11-07)；本节记录决定依据、阻塞和后续顺序，不重复完整操作清单。

### 拓扑决定

- E-pig 继续使用 Netlify 页面/Route Handlers 和 Neon PostgreSQL；消费者入口不迁移到 Oracle。
- DTC 使用 Oracle Always Free 单机，Compose 启动 API、持续 Worker、本机 PostgreSQL 与显式迁移 gate。数据库不发布主机端口，API只绑定本机入口；员工通过 Tailscale 私网访问。
- DTC 每日 `pg_dump --format=custom` 计划写入 Oracle Object Storage；E-pig/Neon 的备份责任单独确认。
- 不采用全 Netlify：当前 Python ASGI 与常驻 Worker 需要重写，属于后端迁移而非部署配置。
- Cloudflare Tunnel 暂不作为首选；需要自有域名或多组织 Zero Trust 时再重新比较 Tailscale。

### 已核对事实

- 起始提交与 upstream 均为 `fcd3d90c2c48b7ee3cd5e560ff1eab995c359e0a`，工作区只有未跟踪 `.work/`。push CI `42233742189` 与 PR CI `42233751742` 已实际成功。
- Netlify 站点 `https://e-pig-au-sandbox.netlify.app/` 的生产售后接口仍为404；Deploy Preview 已有路由，但带正确契约头且缺少 service token 时曾返回`500 INTERNAL_ERROR`，预期为`401 UNAUTHORIZED`。因此当前不能通过 E-pig 发布闸门。
- DTC 本地已有锁定依赖镜像、独立售后 Compose、显式迁移、持续 Worker、健康/就绪/指标入口和一次特定丢响应备份恢复证据；这些不等于生产环境验收。
- 生产 Compose、Tailscale 入口、生产配置预检、PostgreSQL员工凭证管理、生产备份调度、告警和回滚演练尚未实现。
- `oci` CLI 3.96.0、Codex/Claude 的 Oracle MCP 注册存在，但本机无`~/.oci/config`；最近浏览器登录落在风控/注册页面并已停止，没有写凭据。Oracle租户、主区域和免费配额均未确认。

### 上线前实施顺序

1. Oracle账户/租户/主区域/配额解除风控阻塞。
2. Claude按已拆分任务实现生产Compose、Tailscale入口、配置预检、容器构建CI和员工凭证CLI；Codex独立Review、验收后提交。
3. E-pig固定可发布提交，配置Netlify production secrets、Neon迁移并关闭有效token `200`/缺失token `401`闸门。
4. 创建VM、加密块存储、对象存储、Tailscale ACL和最小主机；不开放API/DB公网端口。
5. 先完成配置预检、迁移和恢复演练，再启动API/Worker；执行合成跨系统冒烟、监控、日志、备份年龄和紧急停用验收。

任何一步失败都不发布。当前没有云资源创建、主分支合并、生产secret、真实客户数据、实际部署或真实试用。下一步是实现生产配置基线；Claude Code流空闲阻塞恢复前不由Codex静默接管实现。
