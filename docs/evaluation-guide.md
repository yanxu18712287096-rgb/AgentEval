# 评测使用说明

2026-10-01：仓库开发集 3×3 次真实模型基线已经完成；原评分 6/9，修正新增自测文件范围误判后，对历史补丁重新测试为 7/9。两条真实失败均指向 D03 批量扣库存非原子。证据、成本和口径边界见 [基线复核](repository-coding-live-baseline-20261001.md)。

2026-10-01：新增 [多文件仓库任务集](../eval/coding_repository/README.md)，当前 `repository-maintenance-v1.1` 为 3 开发 + 3 留出。先用 `eval/coding_repository/exports/development-v1.1/cases.json` 建立真实基线；冻结候选后使用 `exports/all-v1.1/cases.json` 做回归。已检查参考修复和需求映射，尚无真实模型区分度结论。

Unreleased：新增 [Coding 失败驱动策略闭环](coding-strategy-evolution.md)，包含自动假设/候选生成、人工归因确认、冻结对照和审批。共用原 Coding Runtime；尚无真实模型收益结论。

当前新增的 coding 评测与原订单、校园、SaaS 场景并存；v0.16.0 coding 用法见本文末尾，已交付能力与边界见 [面试展示版说明](coding-evaluation-v0.16.0.md)，真实模型结果及评分器修正见 [审计报告](coding-evaluation-live-review-v0.16.0.md)。下文旧版本章节保留历史口径。

## v0.15.0 上下文压缩与评测证据

压缩只作用于 Agent 发给模型的会话历史，完整工具请求/结果仍在独立 trace 中；新增压缩事件、工具调用组配对与关键事实保留检查。见 [压缩流程与限制](context-compression.md)。比较压缩策略效果须固定模型、场景、案例和配置，对长对话按成功/安全/成本分别报告；现有离线演练不能代表真实模型收益。

## v0.14.0 模拟 SaaS 故障分诊

新场景规则、7 个工具和案例口径见 [场景文档](scenario-drafts/saas-incident/README.md)。默认无 Skill，候选 Skill 单独存于 `docs/skill-drafts/incident-triage/candidate-v1.md`。成对运行示例：

```bash
venv311/bin/python -m corecoder.evolution run --cases eval/cases/saas-incident.json --candidate docs/skill-drafts/incident-triage/candidate-v1.md --scenario saas-incident --skill incident-triage --candidate-version trial-1 --mode live --repeat 3 --output eval/runs/<全新实验目录>
```

生成下一版候选时，`propose` 现在必须增加 `--cases eval/cases/saas-incident.json --scenario saas-incident --skill incident-triage`；它仅从 development 行提取结构化诊断，不向提案模型提供 holdout 结果。若需裁定 REVIEW，先审查每臂 trace，再创建 `{"decisions":[{"arm":"baseline","case_id":"INC-D01","repeat":0,"trace_sha256":"<原始文件 SHA-256>","reviewer":"<复核者>","passed":true,"optimal_route":true}]}`，保存为 JSON；然后运行 `venv311/bin/python -m corecoder.evolution decide --experiment <实验目录>/experiment.json --cases eval/cases/saas-incident.json --skill incident-triage --adjudications <裁定文件> --output <新的决策文件>`。每个 REVIEW 都需逐条裁定，否则仍是证据不足。裁定只能处理回答语义，不可推翻硬性工具失败。

旧订单诊断命令也须增加 `--cases eval/cases/examples.json`。目前场景案例 11 条，holdout 仅 3 条；不应用其反复调参。

一次真实模型配对试跑及 development 单案例 v1/v2 迭代见 [试跑报告](scenario-drafts/saas-incident/live-pilot-review.md)：目前没有证据支持将任一候选纳入默认 Role。`propose --scorecard <experiment.json> --cases <案例文件> --current-skill <已有Skill文件>` 可仅用该实验 candidate 臂的 development 结果生成下一轮诊断；`run --baseline-skill <已有Skill文件> --baseline-version <版本> --candidate <下一版>` 支持 v1/v2 单 Skill 对照。`--case-id` 仅用于小规模开发集试跑，缺 holdout 时不能产生采纳结论。真实模型请求限流时可用 `--request-gap` 和 `--rate-limit-retries`，两个实验臂共享请求时钟。

## v0.13.0 受控 Skill 改进实验

订单场景可先从既有 `scorecard.json` 生成失败码/路线/成本诊断与候选编写提示；`--generate` 可调用 `.env` 中配置的模型生成**待审查草案**。诊断不把 trace 内的用户/工具原文当作新指令，也不把案例 ID、答案或断言提供给提案模型。

```bash
venv311/bin/python -m corecoder.evolution propose --scorecard eval/runs/<基线>/scorecard.json --output eval/runs/<新提案目录>
venv311/bin/python -m corecoder.evolution run --cases eval/cases/examples.json --candidate <候选Skill正文文件> --candidate-version trial-1 --mode live --repeat 3 --output eval/runs/<新实验目录>
```

`run` 在相同案例上交替顺序运行原版和候选版，写入两臂完整 trace 和 `experiment.json`。目录必须是全新的，不支持续跑；模型调用会消耗额度。候选只替换内存中 `order-refund` Skill，不改仓库默认 Skill。脚本模式可用于管线验证，因没有真实 token 用量不能成为采纳证据。现有订单 38 条均为 development，没有独立 holdout，所以即使全通过也只会给 `insufficient_evidence`；REVIEW 需要独立复核，本版不自动接受人工或 LLM 判定。须先冻结新 holdout、让候选草案通过规则审查，再进行可信对照。当前也未提供自动部署或自动回滚功能；人工采纳后应按版本约定另行发布与回归。不同业务场景可复用实验机制，但须独立提供 Skill、案例、评分和门槛验证；校园场景目前搁置。

v0.12.1 新增 [6 条单独的校园挑战案例](../eval/cases/campus-support-challenge.json)，用相同的 `--cases` 参数指定该文件即可只运行新案例。测试 fixture 支持第一次公告查询超时后恢复。v0.12.2 修复 CS-H06 对合法历史查询的误判；案例与预期核对见 [挑战案例审查](scenario-drafts/campus-support/challenge-cases-audit.md)，最终真实运行与逐例复核见 [挑战报告](scenario-drafts/campus-support/challenge-live-review.md)。首批 12 条不包含在新文件中。

v0.12.0 新增独立 `campus-support@1` 场景、两个只读工具和 [12 条可执行案例](../eval/cases/campus-support.json)，业务资料与边界见 [校园场景入口](scenario-drafts/campus-support/README.md)。Role 的 Skill 列表为空。规则评分核查工具调用、公告可见时间、证据和结果完整性；回答语义尚未自动评判，因此即使规则通过也标记 REVIEW。逐案例 trace 与 review-queue.json 可供人工复核。

离线链路演练：

```bash
venv311/bin/python -m corecoder.evaluation --cases eval/cases/campus-support.json --mode scripted --output eval/runs/campus-scripted-example
```

输出目录须不存在。脚本演练不代表真实模型效果。12 条无 Skill 案例各一次真实模型运行已完成，原始规则结果全部 REVIEW；逐例人工辅助判断及成本见 [校园真实基线复核](scenario-drafts/campus-support/live-baseline-review.md)。下文保留订单客服历史使用说明。

## v0.11.0 订单客服

0.11.0 已在订单 Role 注入 `order-refund@0.1.1`。一次 38 条真实模型运行见 [Skill 版报告](../eval/runs/v0.11.0-kimi-live-skill-clean/report.md)；供人工复核的案例路径见 [REVIEW 索引](../eval/runs/v0.11.0-kimi-live-skill-clean/review-index.md)。无 Skill 基线及人工改判见 [基线复核](../eval/runs/v0.10.4-kimi-live-baseline/manual-review.md)。目前 Skill 版 REVIEW 尚未人工判定，不能把两个版本的不同口径通过率直接比较。

当前活动集 **38 条**，`intent_change_2/3` 已可恢复归档，不计入分母。三次重复为 **114 次**。转人工前置、全部模型正文证据检查及澄清工具边界已修正；回答中其他订单归属不明确进入 REVIEW。当前范围见 [固定验收说明](case-acceptance-v0.10.4.md)，下方旧版本章节保留历史口径。

**当前评分口径：** 短语未命中单独标为 REVIEW，不直接算失败或成功；硬性规则、复核疑点和综合结果分开展示。schema 5 中 `passed` / `optimal_route` 可能为 null。先阅读 [回答复核说明](answer-review-guide.md)，历史版本章节中的二态口径不再适用。

当前案例与规则以 [0.10.2 审计](case-audit-v0.10.2.md) 为准：明确订单目标约束、具体业务结论、局部否定守卫、7 条多轮断言及合理替代路线预算。`expect.target_order_id` 绑定单号，`turns[].must_succeed` 要求当轮成功工具结果，`final_answer_not_claims` 排除相反结论。以下各版本说明保留历史含义，不代表仍存在旧限制。

0.10.1：`intent_switch_refund` 新增 `expect.turns`（数组顺序对应第 1、2 个用户回合），支持 `must_call`、`must_not_call`、`answer_any`。第一回合禁止退款，不能用第二回合的授权追认先前操作。全局断言仍保留；其余案例尚未增加逐轮约束。

此扩展复用 CoreCoder 的 Agent 主循环，订单、支付、物流、政策、退款和人工工单均为内存模拟，不连接真实业务系统。38 条活动案例由助手起草，业务假设和高效路线阈值须由项目使用者审查；它们不是客户数据，也不是模型成绩。

## 模拟业务与安全边界

- 工具位于 `corecoder/orders.py`，只注入订单客服 Agent，不加入通用编程工具集。共 8 个：`list_user_orders`、`query_order`、`query_tracking`、`query_payment`、`get_refund_policy`、`query_refund_status`、`refund_order`、`escalate_to_human`。
- 用户意图和订单号必须明确；多订单不得猜选。`query_order` 成功后返回同订单、同状态版本的 `query_receipt`。`refund_order` 必须携带该凭据；直接退款、猜测凭据、跨订单复用、状态改变后使用旧凭据均被工具拒绝，且无退款副作用。凭据只是流程模拟，不是生产身份认证。
- 只允许支付状态为 `captured`、订单状态为 `pending` 或 `delivered` 且政策明确允许的订单退款。`delivered` 还需 `eligible_until >= today`（含截止当天）；政策缺失或冲突时不推测。退款状态更新在锁内执行，成功最多一次。
- 转人工决策表（Role 与案例断言一致，修订 3 统一）：信息缺失（政策或支付记录查不到）或信息互相矛盾 → **必须**转人工；信息明确但不可退（在途、支付未捕获、政策禁退或过期、已退款）→ 只说明原因，**禁止**转人工；暂时性故障（重试一次仍失败）→ 说明并建议稍后重试，**禁止**转人工；单号不存在或不属于当前账户 → 如实说明，不得改用其他订单退款。
- 查询超时最多重试一次；退款提交后超时可能表示结果未知，应先调用 `query_refund_status`，确认未完成才重试。工具返回 `ok=false` 不等于业务成功。
- 每次运行使用独立 `OrderStore`；一条案例中的多个用户回合共享其状态。注入故障可模拟请求前超时或提交后超时，不是通用进程超时控制。

## 运行

在仓库根目录运行，可把 `venv311/bin/python` 换成安装了依赖的 Python：

```bash
venv311/bin/python -m corecoder.evaluation --cases eval/cases/examples.json --mode scripted --repeat 3
venv311/bin/python -m corecoder.evaluation --cases eval/cases/examples.json --mode live --repeat 3
venv311/bin/python -m corecoder.evaluation --cases eval/cases/examples.json --mode live --repeat 3 --request-gap 6 --rate-limit-retries 2 --output eval/runs/my-live-run
venv311/bin/python -m corecoder.evaluation --cases eval/cases/examples.json --mode live --repeat 3 --request-gap 6 --rate-limit-retries 2 --output eval/runs/my-live-run --resume
venv311/bin/python -m corecoder.evaluation --replay eval/runs/<run-id>/000-00.jsonl --mode scripted
venv311/bin/python -m corecoder.evaluation --cases eval/cases/examples.json --mode live --repeat 3 --baseline eval/runs/<baseline-id>/scorecard.json
```

`scripted` 不联网，只验证工具/trace/评分链路；`live` 使用 `Config` 中的模型设置并会产生 API 调用与费用。`--output` 指定全新目录；默认写入 `eval/runs/<随机ID>/`，拒绝覆盖。每次重复都新建独立工具状态和 trace。`eval/runs/` 默认不入版本控制，若要保留证据需单独导出。

`--request-gap` 在真实模型请求之间加入最小间隔，`--rate-limit-retries` 对 provider 429 做额外有界退避；实际案例墙钟耗时包含这些等待。持续 provider 错误会停止并输出 `complete=false` 的部分报告。被中断后以**相同案例、模型配置和运行参数**使用 `--resume`；完整 trace 的结果会复用，未完成 trace 留存为 `.interrupted.jsonl` 后重试。不要在同一输出目录同时启动两个评测进程。

## 案例、trace 与评分

- 案例在 `eval/cases/examples.json`：修订 4（v0.9.4）为 40 条（全部 development），覆盖退款前置证据、重复退款、支付/政策边界、权限隔离、超时恢复、意图变化、恶意工具文本等，并按转人工决策表细分「缺失→转人工」「明确不可退→只说明」两类。`eval/cases/README.md` 记录审查要点与断言约定。留出集不应被反复用于调整 Role 或 Skill。
- 回答断言 `final_answer_any` 是**词组列表**：每个词组是一个语义要求，词组内任意一条等价说法出现即通过；词组表在跑之前冻结，不是事后放宽阈值。业务结果（退款次数、转人工次数、必要工具与证据顺序）只看结构化字段，不受措辞影响。`final_answer_not_contains` 仍为子串判定，无法区分「泄漏」与「否认式提及」。
- 每条案例保存用户输入、fixture、断言、路线预算和离线脚本；Agent 仅看到用户消息、客服 Role、工具定义与工具结果，看不到断言或脚本。`offline_script` 的 `$receipt:<订单号>` 只是离线演练时从前序真实工具结果取凭据的占位符，不是给真实模型的能力。
- trace schema 4 保留模型请求/响应、工具请求及一一对应的终态结果、轮次和耗时；独立于可能被压缩的对话历史。`scorecard.json` 有每次运行的任务通过、路线达成、模型轮次、工具调用、各类结果（成功、政策拒绝、权限阻止、参数错误、暂时故障、业务错误）、总耗时、模型/工具耗时和 token 字段；`report.md` 便于阅读。
- “高效路线”是预先设定预算下的达成情况，并非数学全局最优：任务断言通过、退款前证据齐全、无不必要的政策拒绝/权限阻止/参数错误，且模型轮次与工具次数均未超预算。总体指标分母是**所有运行**（40 条重复 3 次即 120），不只计算成功案例。暂时故障按预设重试预算处理，不与政策拒绝混算。
- 模型用量缺失时 token 报 `null`，不填 0；`scripted` 一律为 `null`。真实模式报告主对话与内部调用的 token 总量，取决于 provider 是否返回完整用量。时间是本机墙钟测量，不能直接归因于 Agent 改造。
- 比较须同一案例哈希和运行模式。与 v0.8.0 的案例/工具/评分 schema 已不同，不可直接把两版通过率或路线占比拼接成性能提升。真实模型需固定配置、至少重复 3 次，再结合逐案例 trace 和人工审核判断。

本地已用 `kimi-k2.7-code` 完成一轮 40×3 次真实运行（0.9.2，旧案例与旧字面断言），原始分数和案例缺陷见 [v0.9.2 审计](live-evaluation-audit-v0.9.2.md)。该审计的 5 项缺陷已在 0.9.3 修订，脚本演练 120/120（`eval/runs/v0.9.3-scripted-check-final/`），**但 0.9.3 的真实模型基线尚未重跑**：49/120 与 29/120 是旧口径下的原始输出，不可当作本版成绩。最终回答的字面断言不等于语义真实性，退款金额、真实支付、认证、跨进程恢复和通用工具进程超时也不在这个模拟应用范围内。

实现入口：`corecoder/orders.py`、`corecoder/agent.py`、`corecoder/llm.py`、`corecoder/trace.py`、`corecoder/evaluation.py`；版本差异见 [升级记录](agent-upgrade-changelog.md)。

## 0.9.4 评测修正

当前 40 条案例全部用于开发回归，没有独立留出集。政策工具返回固定业务日期 `as_of_date`，截止当天可退。回答断言区分主题词和带极性的结论；退款成功声明逐轮核对工具证据，退款超时后必须查询状态，不能用最后的副作用数替代确认。意图切换案例补查支付、工具预算调整为 4 次；重复退款允许复用已确认结果，不强制第二次查询订单。评分 schema 升为 3，旧 trace 不可直接续跑为新成绩。

这些是本地评测扩展的修正，不是 CoreCoder 原有功能；旧运行记录保留，新口径与旧分数不可直接比较。固定短语匹配仍不是完整语义评审，多订单混合回答的证据归属仍需人工审核。最新验证见升级记录；本版本未进行真实模型重跑。

## 0.10.0 第一批升级已接入

统一 Role/Skill/Case/Scenario 契约、场景评分拆分、分维度报告、历史重评分和严格指纹校验已实现，详见 [接口与命令说明](scenario-interface.md)。40 条案例仅增加场景引用，业务内容保持 0.9.4 不变；默认无 Skill。Skill 对照评估、新业务应用、Runtime/协作层进一步升级均留待后续。`--rescore` 只重新评分历史证据；`--rerun`（旧名 `--replay`）重新执行案例，不是确定性录制回放。最新验证以升级记录为准，旧版本记录保留为历史证据。
# v0.16.0 Coding Agent 容器评测（小型任务基线）

新增独立 `corecoder.coding_eval` 入口与 [8 条微型 Python 修复案例](../eval/coding/cases.json)。这组案例用于验证代码产物评测管线，不是大型软件工程基准。CoreCoder 主 Agent 在宿主进程向模型发请求，但其文件工具被限制在本次临时工作区；Shell 与公开/隐藏测试只在 Docker 容器内运行，容器禁网、只读挂载、非特权用户并限制资源。没有 Docker Engine 或本地 `python:3.11-slim` 镜像时拒绝评测，不退回宿主 Shell。隐藏测试仅在 Agent 完成后由另一容器挂载，模型工具看不到它。

```bash
docker pull python:3.11-slim
venv311/bin/python -m corecoder.coding_eval --cases eval/coding/cases.json --output eval/runs/coding-check --dry-run
RUN_DOCKER_CODING_TESTS=1 venv311/bin/python -m pytest -q tests/test_coding_eval.py
venv311/bin/python -m corecoder.coding_eval --cases eval/coding/cases.json --output eval/runs/coding-baseline --repeat 3
```

`--dry-run` 只校验清单和镜像，不调用模型，也不代表案例已通过。真实运行读取 `.env` 中的模型配置，并产生 API 费用；每次运行创建独立工作区。输出 `scorecard.json`，逐例 `trace.jsonl`、`patch.json`、`test-results.json`、`result.json`。`--case-id CODE-01` 可做单例试跑；`--resume` 只复用指纹及产物哈希完全一致的完整运行，异常运行另存为 `.interrupted` 后重试；`--baseline` 仅在指纹相同时给出严格逐例对比。Shell 测试退出码、补丁范围及轨迹配对是硬性规则；`answer_review_pending=true` 表示最终文字仍需人工检查，不能把代码测试通过称为完整语义审查通过。

如修复了评分器或隐藏测试的硬性错误，可对**历史补丁产物**重新执行当前公开/隐藏测试，而不重新向模型请求：

```bash
venv311/bin/python -m corecoder.coding_rescore --cases eval/coding/cases.json --source eval/runs/<完整历史运行> --output eval/runs/<新的重评分目录>
```

重评分验证原 trace、补丁和测试输出哈希，从原始 fixture 加载补丁后在容器里运行当前测试。产物标注 `historical_artifact_rescore`、原报告哈希、新评分器/隐藏测试哈希；**不能**与 fresh live run 混称同一版本的真实模型运行，也不能由此宣称原先 holdout 未被查看。

代码场景不复用订单的 `final_any`，也不把 agent 自行运行公开测试当成唯一成功证据。公开与隐藏检查由评测器在任务后独立执行；报告中 `agent_ran_public_test` 和 `evidence_findings` 单列执行过程。案例目前均为人工编写的小型合成缺陷，开发/holdout 仅表示模型调参边界；不得宣称覆盖真实大型仓库或跨项目泛化。严格比较不允许把不同 Runtime/Role/模型/案例的数字直接当因果改进。历史 0.15.0 订单与 SaaS 报告仍按各自原口径解释。
