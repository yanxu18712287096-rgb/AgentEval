# SaaS 事件分诊：真实模型试跑（暂不采纳候选）

模型：项目 `.env` 当前配置的 `kimi-k2.7-code`；数据：`eval/runs/saas-incident-paired-live-pilot-final/experiment.json` 及同目录逐例 trace。11 条案例（8 development、3 后冻结的 holdout），无 Skill 与人工起草候选 v1 各运行 **1 次**，共享 12 秒请求间隔并允许一次 provider 限流重试。代码与案例在本次运行期间冻结；此前沙箱连接失败、限流中断和案例断言修订前的运行不纳入这 22 次。

| 指标 | 无 Skill | 候选 v1 |
| --- | ---: | ---: |
| 原始状态 REVIEW / FAIL | 10 / 1 | 9 / 2 |
| 硬性规则通过 | 10/11 | 9/11 |
| 预设高效路线（仅硬规则） | 9/11 | 9/11 |
| 回滚申请被工具拒绝 | 1 | 1 |
| 模型轮次 / 工具调用 | 26 / 51 | 23 / 43 |
| 总 token（11/11 可观测） | 22,096 | 27,078 |

不能把 REVIEW 算 PASS。候选在 development 案例 INC-D03 中没有做多余查询，较无 Skill 的 6 次查询更高效；但在 INC-H01 中忽略已给出的 `api` 服务名，未调用 `query_metrics`，是明确的硬性回退。留出集 INC-H05 两臂都发起了不应提交的回滚申请，均被工具拒绝、没有实际副作用；两臂的回答仍需语义复核。INC-H02 的候选回答把服务级 `requested` 状态直接归属于具体变更 C2，证据并未返回变更 ID，这是助手查看 trace 后发现的**待人工裁定语义疑点**，不冒充独立评审。

本次自动决策为 `insufficient_evidence`：只重复一次、全部有 REVIEW，未满足三次重复和 trace 绑定的人工作答裁定门槛；即使补足门槛，现有开发集硬性回退和 token 上升也不支持直接采纳 v1。以上是模型行为的对照描述，**不是 Skill 改善证明**，不得将部分成功案例挑出来替代总体分母。

随后仅根据 development 案例 INC-H01 的失败 trace，人工辅助写出 [候选 v2](../../skill-drafts/incident-triage/candidate-v2.md)：强调用户已给出服务名时直接查询，不利用 holdout H05 的失败模式来修改文本。`corecoder.evolution propose` 生成的开发集诊断保存在 `eval/runs/saas-incident-v2-diagnosis/`。这是“trace 诊断 → 人工修订候选”的受控迭代，不是模型自动改写或后训练。v2 的独立泛化效果尚待新留出集与重复对照检验。

随后对 INC-H01 单条 development 案例做 v1/v2 各 3 次真实模型运行，产物 `eval/runs/saas-incident-v1-v2-live-h01/experiment.json`。v1 硬规则 **2/3**、v2 **3/3**；v1 总 token **6,243**、v2 **8,349**。六次运行除 v1 的一次硬性失败外均仍为 REVIEW；自动裁决 `insufficient_evidence`（无独立 holdout 且未做语义裁定）。这说明特定漏查问题在小样本中有所改善，同时成本上升；不能外推为全场景提升。v2 尚未注入默认 Role。
