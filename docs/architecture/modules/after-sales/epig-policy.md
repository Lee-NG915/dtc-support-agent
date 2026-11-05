# E-pig 内部演示政策包与市场隔离检索

更新日期：2026-11-05

状态：**当前 synthetic demo 增量已实现并完成本轮验证。18 篇 AU/US/SG 内部自编 demo 政策、来源模型、品牌运行时和下游 Faithfulness 保护已接入；Codex Review 发现的社交绕过、复核截断和陈旧引用问题已修复。固定离线集与最新 DeepSeek Flash 生成节点集均为 34/34；8003 已重启并复验，8002 旧进程未重启。**

本文是当前政策能力说明。实际命令与最终验证结果见 [E-pig 政策验证记录](../../../records/after-sales/epig-policy-v1.md)；产品行为仍以 [售后产品蓝图](product-blueprint.md) 为准。

## 1. 来源与效力

`knowledge/epig/{au,us,sg}/` 各有退货、退款、配送、取消/改单、损坏/错发/缺件、保修六篇，共 18 篇。所有条款均为项目自行编写的 synthetic demo：

- 不是 E-pig 官方政策，未获品牌、业务或法律生产批准，不用于真实订单裁决。
- 不是 Koala 条款改名，不继承其期限、服务名、来源冲突或官方 URL。
- manifest 全部为 `status=demo`、`source_type=internal_demo`、`source_urls=[]`。
- 引用以实际 `source_path`、document/chunk ID、版本和 `source_label=E-pig 内部自编演示政策` 定位，不生成虚构官网。

Koala 原文已移出 active `knowledge/`，本地 `.work/` 归档不参与 ingestion。旧 Koala 文档、报告和 `koala` schema 值只用于历史追溯与旧 payload 兼容，不能作为当前 Agent 政策证据。

## 2. 范围与运行时行为

E-pig 请求必须显式提供 `brand=epig`、`market=au/us/sg` 和 `product_category`；`policy_as_of` 缺省由 `PolicyScope` 统一使用服务端当天，因此直接检索与 HTTP 不会因为入口不同而产生日期语义分叉。未来日期在校验层拒绝，manifest 不接受倒置的有效期。快照生效前日期、无类别或不支持类别失败关闭，不能借用其他市场或其他品牌条款。

`app/config/policy_profiles.py` 集中定义品牌行为。E-pig 非社交 DIRECT 也强制检索，只允许知识检索，禁止通用优惠券、SKU 保修和演示订单写工具；stateless 政策回合既不读取通用历史，也不写入聊天消息历史，避免后续同 session 的 upstream 请求复用政策答案。既有历史数据未做删除或迁移。API 默认仍为 `upstream_demo/global`，避免破坏既有客户端，前端默认品牌不在本次切换范围内。

scope 继续贯穿 ChatRequest、AgentState、ToolContext、BM25、内存向量、Qdrant、Hybrid、缓存和生成。缓存键包含完整 scope 与知识库指纹；指纹和 Qdrant payload 已加入内部来源字段。精确去重同时保留日期、复核规则和来源语义，待确认动作保存发起回合计算出的 scope，不在写入待确认记录时重新取日期。

## 3. 回答与人工复核

- 缺类别、无证据和命中复核条件均由确定性代码处理，不调用生成模型。
- 只有确定性的纯社交文本可在 `GREETING/CHITCHAT` 意图下绕过检索；模型把政策问题误标为闲聊也不能自由生成无引用答案。
- 正常回答剔除与当前问题无关的 review section，避免无关例外导致过度拒答；检索分数最高本身不再被当作例外适用的证据。结构化复核规则在当前 scope 内独立匹配，不依赖普通检索 `top_k`；被截断的命中段会补回，已召回段保持原相对排名。没有结构化规则的旧 review chunk 仍保守转人工。
- 过滤后的 chunks 会写回图状态，Faithfulness 的 checker、改写和降级只使用这组证据；过滤后没有普通证据时直接拒答，不调用模型。成功改写会同步替换 citation IDs，避免新答案沿用旧证据。
- 正常、Offline、模型 fallback、Faithfulness 改写/降级和拒答文案均由代码附加 demo 声明，不依赖模型自觉。checker 只评估移除确定性声明后的实体答案，避免将声明误判为模型幻觉。
- 损坏、错发、缺件材料包括订单号、商品/SKU、购买或交付证明、商品与包装照片及人工评估下一步；不自动承诺退款、更换或外部操作。
- 定制/最终销售、已使用/污渍/破损退货、偏远配送、发货后取消、费用返还、意外损坏和责任争议按 manifest 的复合规则转人工。普通期限、退款时效、材料和常规配送不应被无关规则误拦截。

## 4. 当前验证与未验收范围

已实际通过静态政策检查、**242 项全量测试（覆盖率 80%）**、34 条固定离线集、最新 34 条 DeepSeek Flash 生成节点集，以及重启后的健康检查和 AU 真实 HTTP 冒烟；此前 AU/US/SG 三市场冒烟亦已通过。真实生成曾出现 32/34、两轮 33/34 和 fallback，最新通过不消除模型波动。配置范围 mypy（`app`、`eval`）通过 125 个源文件，原 provider 类型错误已修复；详细报告、Review 与服务状态见验证记录。

本增量不代表完整 LangGraph 固定集、Faithfulness 固定集、生产 PostgreSQL/Redis/Qdrant、真实 E-pig 联调、生产政策或所有自然语言问题验收。真实订单接入前必须由可信服务端订单上下文派生品牌、市场和日期，并完成业务/法律批准及历史版本建设。
