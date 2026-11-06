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

模型质量仅在实际真实输出与评分执行后讨论。零调用、纯规则review门控、预算截断和错误分别列出，报告质量完整度full/partial/none；配置了real不等于质量已测。关键词命中只说明词面覆盖，旧e2e的`legacy_answer_keyword_proxy`不证明实际工具名、参数或调用次数正确，真实工具trace另行验收。生成成功也不等于已获人工批准或工单已核验。

## 报告与复算

报告写入忽略目录`eval/reports/{task}_{UTC}.json`及latest索引，不默认入Git。检查run中的mode/provider/model、实际task_coverage、measured/not_measured、源码commit与工作区digest/dirty说明、数据/知识digest和prompt版本。未提交代码不属于报告中的HEAD，需结合候选digest/最终提交核对；Git不可用写unknown。报告不存凭据、数据库URL或完整设置。

涉及DB的e2e只操作本次新临时SQLite，容器构建失败/取消和正常结束都关闭已取得资源，再清理所属目录。临时报告不承诺长期保留；稳定记录保存版本、命令、结果与失败边界，不删除旧失败。

## 已测量与可以使用的结论

以[当前流程记录](records/after-sales/dtc-workflow.md)的版本和运行范围为准。Fake、规则/检索、真实模型、HTTP/PG、浏览器、真实试用各自证明对应层次；不要合成一个“Agent通过率”。历史Koala/Kimi、DeepSeek生成节点、CE与旧问答数字保留各自来源，不能转作本fork的新收益或完整图成绩。

## CI与变更验证

先选受影响单元/集成检查；权限、事务、模型入口或依赖有实质变化时跑必要全套。政策/知识变更执行epig-policy及相关检索检查，prompt变更冻结候选并运行开发回归；真实模型和保留集需独立配置/记录。CI没有运行不写通过，本地工程测试不替代浏览器或业务验收。

上一章[09 · 安全](09-security.md) · 下一章[11 · 部署](11-deployment.md)
