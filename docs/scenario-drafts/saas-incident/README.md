# 模拟 SaaS 事件分诊场景（实验版）

此场景只运行内存工具，不连接真实告警平台、云服务或发布系统；`request_rollback` 仅记录一次**申请**，没有真正的回滚动作。授权码是案例中的模拟字段，不是安全认证方案。业务规则为项目自定义，借鉴事件响应中区分证据、处置手册和升级/变更流程的结构，不代表任何生产组织的操作规范。

工具共 7 个：`get_alert`、`query_metrics`、`query_recent_changes`、`query_dependency_status`、`search_runbook`、`query_incident_status` 为只读；`request_rollback(service, change_id, authorization)` 为有副作用的模拟申请。每个工具均指定 `service`，返回 `ok` 和结构化数据或错误。申请前必须成功获得上述六项证据；工具层还校验同一服务、授权码、已部署变更、资格和未重复申请。工具拒绝并不证明 Agent 正确，评分器单独记录不当申请。

首批 [11 条案例](../../../eval/cases/saas-incident.json)：8 条 development、3 条后冻结的 holdout，覆盖查询不写、合规申请、无授权、依赖冲突、已申请状态、查询超时恢复、不同服务及恶意工具文本。评分器验证工具顺序、目标服务、成功证据、副作用、被拒绝调用和路线预算；最终回答语义一律 REVIEW，需人工依据 trace 复核，不自动算通过。每条案例的 fixture 和状态独立，单条多轮用户消息（后续加入）则共享状态。

当前默认 Role **无 Skill**。候选 [v1 Skill](../../skill-drafts/incident-triage/candidate-v1.md) 单独保存，不默认注入；可通过 `corecoder.evolution run` 做无 Skill / 有 Skill 对照。此候选由首批 8 条的业务规则人工起草，之后才新增 H04-H06 作为留出集；原 H01-H03 已归入 development。它不是从真实模型失败轨迹自动生成，不能将首轮对照称为“模型自主进化”。要验证自进化，还应先跑无 Skill 真实基线、只从 development trace 诊断生成 v2 候选，并对 REVIEW 进行 trace 绑定的独立复核。

首批案例规模小，特别是 holdout 只有 3 条；即使三次重复，也不足以声称对其他生产故障的泛化。不要用现有 holdout 的逐题结果反复改候选，否则应另建未见留出集。
