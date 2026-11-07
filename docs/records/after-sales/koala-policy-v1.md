# Koala 政策包与市场隔离检索：验证记录

验证日期：2026-11-05

状态：**本地未提交候选改动已完成离线及真实生成节点固定评测；多轮独立 Review 发现的缺陷均已实现修复、加入回归并由独立复查实际复验关闭。本文不代表已提交、已推送、完整图或 Faithfulness 验收、生产政策批准、生产集成或 E-pig 联调通过。**

对应实现与使用边界见 [Koala 政策包与市场隔离检索](../../architecture/modules/after-sales/koala-policy.md)，跟踪 Issue 为 [DTC #3](https://github.com/Lee-NG915/dtc-support-agent/issues/3)。

> 历史限定：下述“最终通过”“缺陷关闭”和“指定范围内无剩余阻塞”只描述当时已移除的 Koala 候选及固定样例，不是对当前代码或所有自然语言问题的持续保证。2026-11-05 Codex 对当前 E-pig 实现发现的新反例及修复见 [E-pig 验证记录](epig-policy-v1.md)。

## 1. 验证对象

- 12 份 Koala AU/US 中文演示摘要和 `knowledge/policy-manifest.json`；SG 无政策文件。
- 品牌、市场、商品类别和政策日期过滤，以及缓存、Hybrid、Self-RAG、工具、引用与生成保护。
- 按冲突章节和结构化复合查询规则触发的人工复核；跨文档、释义和正常问题对抗样例。
- 固定 29 条 Koala 离线检索/保护样例，以及同一数据集上的真实 `generate_node` 生成评测。
- 既有通用 65 条检索样例、当前工作区全量应用测试、覆盖率、格式、lint、文档和售后契约检查。

本轮没有执行真实外部写入，没有使用真实客户数据，也没有修改另一个 E-pig 工作区。真实模型评测只生成合成政策问题答案，不创建订单、退款、退货、取消或工单。

## 2. 最终实际通过的检查

以下命令使用项目 `.venv` 在 macOS、Python 3.14.3 环境执行：

| 命令 | 实际结果 |
|---|---|
| `.venv/bin/python scripts/check_koala_policy.py` | 通过：12 份 AU/US 文档，8 份文档包含人工复核配置，SG 无文档；该命令未抓取官方 URL |
| `.venv/bin/python scripts/check_project_docs.py` | 通过：28 个受管 Markdown 文件、9 个开发技能；仅结构检查 |
| `.venv/bin/python scripts/check_epig_after_sales_contract.py` | 通过：OpenAPI、fixtures、ID、请求头及写入约束 |
| `.venv/bin/ruff format --check .` | 通过：170 个文件已格式化 |
| `.venv/bin/ruff check .` | 通过 |
| `git diff --check` | 通过 |
| `.venv/bin/python -m pytest -q tests/unit/test_policy_scope.py tests/unit/test_action_confirmation.py tests/unit/test_koala_policy_eval.py tests/integration/test_koala_policy_retrieval.py tests/e2e/test_koala_policy_api.py` | **42 passed**，覆盖 Koala 检索、生成、工具、HTTP、确认范围、结构化复核规则和真实生成评测评分逻辑 |
| `.venv/bin/python -m pytest -q --cov=app --cov=eval --cov-report=term:skip-covered --cov-fail-under=70` | **217 passed**，1 个既有 LangGraph pending deprecation warning；总覆盖率 **74.22%**，通过 70% 门禁 |
| `.venv/bin/python -m eval.runner --task koala-policy` | **29/29 passed**；范围、来源、目标文档、首证据、必要条款和查询感知人工复核失败数均为 0 |
| `.venv/bin/python -m eval.runner --task koala-policy --llm` | 离线部分 **29/29**；真实生成节点部分 **29/29**，模型 `kimi-for-coding`，**13 次模型调用、16 条确定性保护、0 fallback** |
| `.venv/bin/python -m eval.runner --task retrieval` | 65 条既有样例：词法重排 Precision@1 **0.8308**、Recall@3 **0.9077**、MRR **0.8744** |

最终 Koala 报告写入：

- `eval/reports/koala-policy_20261105T023644Z.json`
- `eval/reports/koala_policy_latest.json`

通用检索报告写入 `eval/reports/retrieval_20261105T023142Z.json`。报告时间为 UTC；本地验证日期为 2026-11-05。

Koala 29 条覆盖 AU/US 保修、退货、退款、配送、取消、损坏和缺件，以及 AU 快照前日期、US 不支持的床垫类别、US 缺商品类别和 SG 无政策。普通期限、White Glove、配送费支付和退货材料明确期望不触发人工复核；最低试用期、退款时间、Premium Delivery、取消冲突、跨文档退款冲突及释义变体明确期望触发。

离线任务使用 `bm25+hash-vector+rrf+lexical-rerank+scoped-cache`，报告记录 `model_generation_tested: false`。真实生成任务在同一固定检索证据上直接调用 `generate_node`：正常样例要求一次模型调用，冲突和无证据样例要求零模型调用。报告分别记录 `full_graph_tested: false` 和 `faithfulness_tested: false`，不能与离线结果或完整 Agent 验收混为一项。

真实生成报告不保存完整回复，只保存拒答、引用、必要条款、模型调用次数、fallback 和 citation chunk IDs。`required_answer_term_groups` 只用于接受“五年/5 年”等等价答案措辞；离线证据仍按原 `required_terms` 严格检查，没有同步放宽政策证据要求。

通用 65 条检索使用 Hash 向量和词法回退；本地 Embedding 与 Cross-Encoder SDK 未安装，因此没有把这些数字描述为真实向量模型或 Cross-Encoder 成绩。

## 3. 失败、修复与复测历史

### 初版检索与首轮独立 Review

以下数字是历史过程结果，不是当前最终结果：

1. 首轮 Koala 固定评测为 **16/20**。词法重排改为中文词元及标题/正文加权后，四个首证据主题错误进入回归；初版复测为 **20/20**。
2. Self-RAG 测试桩未接收新增 `scope` 曾导致 **186 passed / 6 failed**；更新测试桩并增加透传断言后恢复通过。
3. 首轮独立 Review 发现 DIRECT 绕过、空 Qdrant 清理、缺商品类别、未来日期、文档级 review、市场来源校验、fallback、degraded 假空缓存、fingerprint 和工具截断等问题。各项均有实现修复和自动化回归。
4. 关联修复包括：待确认动作绑定完整 `PolicyScope`；422 错误安全序列化；未传日期与未来日期统一使用本地日历；复核查询忽略英文服务名空白差异。

### 修复后独立复查：复核门控仍有阻塞

独立复查实际复跑聚焦测试并使用对抗查询，发现原“首证据所属文档”设计仍不正确：

| 复查缺陷 | 修复与回归证据 |
|---|---|
| 跨文档冲突漏拦截：“家具损坏后多久能退款”首证据是损坏段，退款冲突排第二 | `policy_review_chunks()` 在全部召回文档中按主题选择冲突证据；门控和引用共用该组 chunk，固定样例验证引用进入 `refunds` |
| 释义绕过有限词表：“床垫需要保留 10 晚才能退吗”“退货后啥时候能收到钱” | 增加保留时长、收到钱等复合规则；首冲突段严格强于全部非冲突证据时保守兜底；两个原样例均进入固定集 |
| 孤立宽泛词误拦截：“至少”“最少”“已使用”“配送费” | `review_query_rules` 改为按主题的 `all_of` 复合条件，组内才是同义备选；配送费支付、退货材料、价格和清洁查询均验证不触发 |
| 空白 review term 匹配任意查询 | `ReviewQueryRule` 拒绝空白 topic、空 group 和空白 term，并有负向测试 |
| review section 标题漂移静默关闭保护 | ingestion 校验配置的 section 确实存在，缺失即失败，并有临时知识包负向测试 |

正常回答还会剔除未触发复核的冲突段，避免真实模型看到无关冲突后自行过度拒答；Koala 生成温度设为 0。

### 再次独立复查：同分释义与 Offline 证据回流

再次复查确认跨文档、原误拦截、引用和元数据透传已改善，但又复现两个阻塞：

1. **同分释义漏拦截**：“床垫要用多久才能退”首冲突段与普通段同分，严格最强兜底不适用；“已经用过的沙发能不能退”缺少“用过”同义表达。已增加试用时长与退货的复合规则及“用过”，两个同分变体进入固定 29 条，没有恢复“所有同分首冲突段都拦截”的过度规则。后续对照复查又发现“床垫退回用哪家快递，多久能到”会被单字“用”跨分句误拼接，规则已收紧为“用多久/使用多久/保留多久/试用多久”等紧密短语加退货上下文，并加入该正常物流问题的 `refused=false` 回归。
2. **Offline 冲突段回流与引用丢失**：生成层过滤了局部 `chunks`，`_offline_answer()` 却重新读取原始 state。现在由 `generate_node` 显式传入过滤后的 chunks；Faithfulness 的既有调用保持兼容。正常对抗测试除 `refused=false` 外，还断言正文不包含“以下来源差异必须交由人工”、引用非空且全部来自非 review chunk。

最终独立复验实际确认：“床垫要用多久才能退”正确触发人工复核并引用 AU returns 冲突段；“床垫退回用哪家快递，多久能到”和“退货至少需要准备什么材料”均不误拦截，Offline 正文不含冲突提示，引用非空且全部来自普通 chunk。复查结论为：两个同分释义/Offline 阻塞及新增物流误拦截均已实际复验关闭，本轮指定范围内无剩余阻塞。

### 真实生成评测的失败与修复

真实模型评测没有把离线通过当作生成通过：

1. 初次 22 条运行为 **21/22**，AU 损坏沙发缺少必要条款；第二次为 **19/22**，报告记录缺失“人工评估”“缺少的商品/SKU”“五年”等词。
2. 评测增加缺失词诊断；对“五年/5 年”和缺少商品/SKU 的等价表达使用答案侧备选词组，离线证据检查保持不变。
3. 扩充 27 条后真实生成曾为 **26/27**，AU 损坏沙发漏掉“照片”。随后在提示词中明确要求保留损坏、错发、缺件问题的相关材料和人工处理下一步，Koala 温度降为 0；27 条连续两次达到 **27/27**。
4. 加入最后两个同分释义样例后，最终 29 条真实生成节点评测为 **29/29**，13 次模型调用、0 fallback。失败报告保留在 `eval/reports/`，最终报告见上文。

## 4. 未通过与未验证范围

### Mypy 仍有既有错误

`.venv/bin/mypy` 检查 124 个源文件，本轮新增的评测类型错误和变量重定义已修复，最终仍只有：

```text
app/providers/deepseek.py:53: error: No overload variant of "int" matches argument type "object" [call-overload]
```

该文件不在本轮改动范围内，因此不能宣称完整 CI 已通过。其余本地门禁通过不消除这个阻塞。

### 独立复验范围

多轮独立 Review 均产生了实际缺陷清单，当前每项都有对应实现、回归和最终只读复验。最后复验明确关闭同分释义、Offline 证据回流和物流误拦截，指定范围内无剩余阻塞。该结论不等于独立审查者已验证完整图、真实生成模型、生产 Qdrant/Redis 或所有自然语言问题；这些范围仍如下文单独保留。

### 真实生成评测不是完整 Agent 或生产验收

本轮已调用真实 `kimi-for-coding` 生成模型，但只直接测试 `generate_node` 和固定检索证据。没有执行完整 LangGraph 的真实模型路由、规划、工具、Self-RAG/Faithfulness 改写或端到端评分，也没有由独立裁判证明所有答案语义忠实。29/29 不表示所有自然语言问题、模型答案或引用都“没有问题”。

### 没有重新抓取全部官方 URL

政策内容来自本轮官方公开页面调研，但上述静态检查命令不会访问网络。当前网页快照也不能自动证明历史订单适用条款。所有条目仍为 `demo`，没有品牌授权、业务/法律批准或生产发布批准。

### 没有完整生产集成验收

未进行真实 E-pig 联调、真实消费者订单验证、生产 Qdrant/Redis 部署验收或真实外部工单写入。Koala 请求范围字段目前只是演示/测试入口，生产必须由服务端可信订单/会话上下文派生。完整售后工作台、持久审批、Connector、对账和页面仍在计划阶段。

## 5. 当前结论

当前候选可以作为**公开来源演示政策包、已完成离线及真实生成节点固定评测的隔离检索增量**继续评审。已有证据证明固定样例和当前自动化测试满足预期，多轮 Review 的已报告缺陷均有实现、回归和独立关闭结论；但完整图、Faithfulness、所有市场政策和生产售后流程仍未验收。本任务尚未获得新的提交/推送授权，工作区改动保持未提交。
