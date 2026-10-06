# Coding 策略改进闭环（Unreleased）

本次在已有 v0.16.0 Coding 评测器上新增受控工作流，不是重训模型或自动修改生产配置。
原业务场景 `corecoder.evolution` 保留；新入口为 `python -m corecoder.coding_evolution`。

## 分工与状态

1. `collect`：程序校验源 result、trace、patch、测试产物哈希，只收集开发集失败，排除运行/基础设施错误。
   若输入是历史补丁复验报告，还会绑定原始 scorecard 与运行产物、用当前评分器重新核查复验结果；只忽略 `unittest` 报告中不稳定的耗时文本。
   模型只见白名单事件和失败类型，不见隐藏测试输出。轨迹可能含任务代码，人工仍须检查候选是否过拟合。
   单事件最多 6000 字符，整批事件最多 64000 字符，每例最多 80 条；截断标记明确，完整原文保留在源 Trace。
2. `hypothesize`：LLM 自动生成根因、替代解释、假设、适用范围、预期收益、风险和证据 ID。
   审计源路径和案例 ID 不直接加入请求，留出集不加入生成请求。
   对模型可见的每条事件再限长为 1200 字符并标记截断，完整源证据不变。Kimi 提议生成使用独立的 16384 输出 token 上限和 JSON 格式约束；空正文及无效 JSON 会报明诊断并停止，不会保存伪造的提议。
3. `confirm`：人工阅读原始证据后确认策略问题，写入审查者和理由。系统不代替用户确认。
4. `generate`：LLM 根据已确认假设自动生成候选 Skill，审批链与内容哈希一起冻结。
5. `run`：从干净工作区重新执行 Agent，旧/新策略交替，至少三次重复，共用同一 Runtime。
   每次新建模型实例，限速时钟共享但对话不共享；此版串行，不是完整并发状态隔离。
6. `decide`：重新检查产物、策略绑定、案例、环境指纹。缺失运行、非真实模型、Token 缺失或非 Skill 漂移均判证据不足。
   任意对应运行由成功变失败则拒绝；无回归且客观成功数提升，或总 Token 至少下降 10%，仅建议人工审查。
   该阈值是启发式筛选，不是统计显著性检验，也不意味着所有成本维度改善。
7. `review`：人工核实回答诚实性、适用范围和证据，接受或拒绝；接受前重新判定，不能覆盖硬门槛。
   输出审批凭据，不自动激活。普通 Coding CLI 可通过 `--strategy candidate.json` 显式加载候选。

文件独占创建，禁止覆盖旧版本。哈希检测意外漂移，不是签名/身份认证；reviewer 是本地人工声明。
源失败策略必须与 baseline 相同；实验开发集必须与生成证据时相同。普通严格比较入口不放宽。

## 命令示例

路径仅为示例，先准备现有 `eval/runs` 父目录和真实失败报告。没有开发集失败就停止，不制造进化收益。
`hypothesize`、`generate`、`run` 使用环境中的模型配置，会产生费用；中间人工确认步骤不可跳过。

```bash
venv311/bin/python -m corecoder.coding_evolution collect --cases eval/coding/cases.json --input eval/runs/baseline/scorecard.json --output eval/runs/evidence.json
venv311/bin/python -m corecoder.coding_evolution hypothesize --input eval/runs/evidence.json --output eval/runs/hypothesis.json
# 人工审查 hypothesis 及其引用的原始 trace 后，填写实际核查结论。
venv311/bin/python -m corecoder.coding_evolution confirm --input eval/runs/hypothesis.json --reviewer YOUR_NAME --rationale '实际归因核查结果' --output eval/runs/confirmation.json
venv311/bin/python -m corecoder.coding_evolution generate --input eval/runs/confirmation.json --version trial-1 --output eval/runs/candidate.json
venv311/bin/python -m corecoder.coding_evolution run --cases eval/coding/cases.json --input eval/runs/candidate.json --repeat 3 --output eval/runs/paired-trial-1
venv311/bin/python -m corecoder.coding_evolution decide --input eval/runs/paired-trial-1/experiment.json --output eval/runs/decision.json
# 逐例核实回答及证据后选择 accept 或 reject；无收益、回归、证据不足不能接受。
venv311/bin/python -m corecoder.coding_evolution review --input eval/runs/paired-trial-1/experiment.json --reviewer YOUR_NAME --rationale '实际审查结果' --decision accept --output eval/runs/review.json
```

已有策略升级：先用 `coding_eval --strategy 当前candidate.json` 生成基线；实验传 `--baseline 当前candidate.json`。
候选可以显式复用或回退，但没有后台策略指针更新。

## 边界

2026-10-02 当前进度：仓库开发集历史真实模型基线复核为 7/9；从两条真实失败生成[假设](../eval/runs/repository-dev-baseline-rescore-v3-20261001/hypothesis.json)，用户确认归因后生成候选 trial-1。6 个任务 × 3 次 × 2 臂的[成对回归报告](coding-evolution-paired-trial-1-20261002.md)已完成，判定 `reject`：开发集基线 5/9、候选 8/9，留出集基线 8/9、候选 6/9，存在两项配对回归且 token 大幅上升。候选未激活。`eval/runs/` 默认忽略，若在其他机器复现须单独传递冻结产物。

验证记录（2026-09-30）：`RUN_DOCKER_CODING_TESTS=1 venv311/bin/python -m pytest -q` 为 **514 passed**，包括 22 项新增契约/集成测试。Docker 场景使用 scripted 模型，只验证基础设施与注入路径，不作为模型效果证据。语法编译与 CLI 帮助检查通过，Ruff 未安装。

- 本次接通代码链路并做离线验证，没有运行真实模型或激活正式策略。
- 8 条原微型任务不变，只作冒烟；其 holdout 已查看，不能作为未触碰的泛化证据。
  新增 [多文件合成任务集](../eval/coding_repository/README.md) 为 3 开发 + 3 留出，已做案例审计但仍需真实模型验证区分度，不能用旧 8 条宣布自进化有效。
- 人工须保证留出集没用于策略生成或调参；程序过滤无法证明人的盲审状态。
- 中断保留 manifest、receipt 和不完整 experiment，不自动续跑，重跑须用新目录。
- 不含跨进程 Runtime 恢复、异步分支重构、自动部署、外部 Agent 通用适配或显著性检验。
- 生成器无工具权限，提示要求把 Trace 当数据，但不保证模型绝对免疫提示注入。
