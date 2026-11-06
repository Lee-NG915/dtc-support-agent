# 10 · 评测与回归

更新日期：2026-11-06。当前入口默认离线；工程守卫、Fake流程、真实模型质量和实际试用分别记证据。完整项目进度见[项目方案](project-plan.md)，实际验证见[流程记录](records/after-sales/dtc-workflow.md)。[旧测评报告](14-evaluation-report.md)与[MEASURED](../eval/MEASURED.md)保留历史来源，不作为本fork的新成绩。

## 命令与数据集

在项目根目录、安装原锁定开发依赖后运行：

```bash
.venv/bin/python -m eval.runner --task intent
.venv/bin/python -m eval.runner --task retrieval
.venv/bin/python -m eval.runner --task epig-policy
.venv/bin/python -m eval.runner --task generation
.venv/bin/python -m eval.runner --task e2e
.venv/bin/python -m eval.runner --task all
```

默认使用显式`Settings(_env_file=None)`、HashEmbedding、词法重排、Offline/Fake及新临时SQLite；不读取应用.env、不继承已有数据库/Provider/EPIG目标，不下载本地模型。generation默认明确未测质量，e2e只验证离线图能运行；all执行intent/retrieval/generation/e2e，**不包含epig-policy**，需另运行对应命令。报告的task_coverage以实际执行为准。

| 文件 | 作用与边界 |
|---|---|
| `intent_100.jsonl` | 既有意图开发集；默认规则/覆盖层，不是本fork真实模型保留集 |
| `rag_eval.jsonl` | 既有65条检索回归；离线Hash向量/词法与真实embedding/CE分别报告 |
| `epig_policy_eval.jsonl` | 34条AU/US/SG内部演示政策开发回归；不是holdout或完整Agent成绩 |
| `e2e_eval.jsonl` | 既有16条upstream_demo问答；不是员工售后六故事的整体验收 |

10个业务开发/演示种子与12条独立保留集的规划在[流程设计](architecture/modules/after-sales/dtc-workflow.md)。后者须在prompt/知识/参数冻结后准备；若用其失败调参，该集转为回归集，另建保留样例。

## 真实模型的显式入口

当前项目尚未确认真实评测Provider、模型与累计预算，默认不执行。旧`--llm`单独会安全失败；不能再用它或应用.env隐式启用收费调用。

当前编码支持DeepSeek显式路径，运行前必须确认以下配置：

| 配置 | 含义 |
|---|---|
| `--allow-real-model` | 明确进入真实调用路径；省略时保持离线 |
| `--provider`、`--model` | 明确本次已确认的Provider/模型，不根据机器现有配置猜测 |
| `--max-cases`、`--max-calls` | 案例和模型调用数量上界，必须为正数 |
| `--max-output-tokens` | 单次输出token上界；这些是调用限制，不是Provider硬计费上限 |
| `DTC_EVAL_DEEPSEEK_API_KEY` | 专用凭据，仅显式真实路径读取；不读应用DEEPSEEK_API_KEY/.env，禁止贴入对话或报告 |
| `DTC_EVAL_DEEPSEEK_BASE_URL` | 可选的专用Provider地址；不作为业务Connector目标 |

先核对模型、当前计费和预算，再配置专用环境变量。真实路径的运行还需逐例核对事实/引用/可执行性，不能仅靠同模型裁判。当前真实成本、质量、Faithfulness和用户效率均未由本次离线结果证明。

## 指标怎么算

检索按实际document/chunk匹配计算Precision@k、Recall@k、MRR；准确率需要同时给分子、分母和集合版本。规则检索成绩可以报告，不能把Hash向量叫真实embedding或把词法重排叫Cross-Encoder；本入口默认不会因装了模型依赖而自动加载CE。

模型质量仅在实际真实输出与评分执行后讨论。零调用、纯规则review门控、预算截断和错误分别列出，报告质量完整度full/partial/none；配置了real不等于质量已测。关键词命中只说明词面覆盖，旧e2e的`legacy_answer_keyword_proxy`不证明实际工具名、参数或调用次数正确，本批新增实际Tool.run轨迹，范围见下节。生成成功也不等于已获人工批准或工单已核验。

## 实际工具轨迹

当前e2e为本次自有container.registry安装窄代理，在图执行前捕获真实Tool.run。报告的`agent_tool_calls`给出每例工具名、Schema规范化参数、状态与汇总次数；不记录context、完整结果或异常正文，Connector调用另列未测，不混算。旧关键词proxy保留历史含义，不替代这些轨迹。

可选`expect_tools`仅支持工具name/arguments/count的精确多重集合比较；未声明期望则准确率为null。脱敏或截断改变参数的案例明确不可评分，不让两个不同URL或长参数前缀相同误判相等，分母只含可评分且实际执行的案例。正向选择与正确不调用分别计数；真实已测但全错仍为0分，不能改标未测。当前只运行离线数据，不声称真实模型工具选择质量。

代理只解析一次、保持原执行与异常行为，结束恢复原工具对象；自定义覆盖run的工具明确拒绝安装，避免悄悄改变行为。当前内置工具均沿用基类run。源码入口为`eval/tool_trace.py`与`eval/runner.py`的eval_e2e，反例回归在`tests/unit/test_eval_tool_trace.py`。这些计数来自既有upstream_demo集合，仍不是员工售后六故事的质量成绩。

## 报告与复算

报告写入忽略目录`eval/reports/{task}_{UTC}.json`及latest索引，不默认入Git。检查run中的mode/provider/model、实际task_coverage、measured/not_measured、源码commit与工作区digest/dirty说明、数据/知识digest和prompt版本。未提交代码不属于报告中的HEAD，需结合候选digest/最终提交核对；Git不可用写unknown。报告不存凭据、数据库URL或完整设置。

涉及DB的e2e只操作本次新临时SQLite，容器构建失败/取消和正常结束都关闭已取得资源，再清理所属目录。临时报告不承诺长期保留；稳定记录保存版本、命令、结果与失败边界，不删除旧失败。

## 已测量与可以使用的结论

以[当前流程记录](records/after-sales/dtc-workflow.md)的版本和运行范围为准。Fake、规则/检索、真实模型、HTTP/PG、浏览器、真实试用各自证明对应层次；不要合成一个“Agent通过率”。历史Koala/Kimi、DeepSeek生成节点、CE与旧问答数字保留各自来源，不能转作本fork的新收益或完整图成绩。

## CI与变更验证

先选受影响单元/集成检查；权限、事务、模型入口或依赖有实质变化时跑必要全套。政策/知识变更执行epig-policy及相关检索检查，prompt变更冻结候选并运行开发回归；真实模型和保留集需独立配置/记录。CI没有运行不写通过，本地工程测试不替代浏览器或业务验收。

上一章[09 · 安全](09-security.md) · 下一章[11 · 部署](11-deployment.md)

## 首批独立保留样例（已准备，未运行）

[after_sales_holdout_v1.jsonl](../eval/dataset/after_sales_holdout_v1.jsonl)由Codex验收侧在当前业务prompt/知识冻结后准备，共12例：完整政策图6例、员工建议2例、工程守卫4例；[冻结清单](../eval/dataset/after_sales_holdout_v1.manifest.json)保存基线提交及203个app/knowledge文件的SHA-256。这里只准备了输入与人工验收断言，尚未接入本批通用CLI或运行真实模型，不能直接把该文件当旧epig-policy数据格式加载。

评测前须完成专用合成fixture适配、人工0/1/2评分与Provider/model/预算配置。政策图应记录检索、生成及Faithfulness各自结果，员工建议记录引用与仅editing边界，工程守卫记录真实API/进程/写入次数，三组不合并LLM准确率。使用失败调参时必须降级为开发回归集并另建保留集。初次实际运行前回核冻结清单；未运行就不填写通过数。
