# AgentEval｜多场景 Agent 运行与可追溯评测回归框架

一个 Agent 说“已经完成”，并不等于任务真的完成了。它可能先试了一次本应避免的退款调用，靠工具拒绝后才补查订单；也可能正确分析了代码缺陷，却没有提交任何修改。只保存最终回答，看不到这些过程。

AgentEval 是一个本地实验台：给 Agent 一个可重复的任务和初始状态，记录每轮模型与工具交互，再用场景自己的规则验收。改动提示词或 Skill 后，可以在同一批任务上重新运行，比较成功、退化和成本。它面向想研究 Agent *如何*完成任务的人，也能作为接入新场景的基础。

## 看一个具体例子

在“会议室预约改期”任务里，公开需求要求正确处理时区、预约首尾相接，以及改期失败后保持原记录不变。一次真实模型运行中，Agent 读了源码并准确指出这些缺陷，随后直接输出了修复计划，却没有修改文件。公开测试仍然通过，因为初始代码本来就能通过基础用例；独立验收发现补丁为空、隐藏测试失败，最终判为失败。

这条案例说明了项目如何把一次运行拆成可检查的证据：**任务要求 → 模型决策 → 工具调用 → 工作区变化 → 测试结果 → 判分**。评分看的是实际交付，Trace 用来解释为什么没有交付。[完整实验复核](docs/coding-evolution-paired-trial-1-20261002.md)记录了这类失败在策略对照中的位置。

## 框架怎么工作

每条案例给出用户请求、初始环境和可执行的预期。运行时创建独立状态或工作区，按 Role 提供工具；Skill 是可选的执行建议。模型提出工具调用后，Runtime 执行并把结果交还模型，同时将调用、返回、异常和耗时写入 JSONL Trace。工具调用与结果使用 ID 配对；会话压缩不会改写已经保存的评测证据。

运行结束后，场景评分器独立验收。订单售后场景使用 38 条活动案例，检查是否查对订单、取得退款前置证据、正确处理超时和意图变化。Coding 场景使用 6 个多文件合成仓库任务：Agent 可读公开需求与公开测试，评测器在任务结束后运行隐藏测试，同时检查补丁范围和执行完整性。模型轮次、工具调用、Token 与耗时单独统计，不把“做成了”和“做得省”混为一项分数。

Coding 评测的终端命令及测试运行在受限 Docker 容器中；文件工具只能操作当前案例的临时工作区。普通交互 CLI 与 MCP 服务有各自的权限边界，不使用这套 Coding 容器环境。Role、Skill 和常规业务 Case 有公共接口；Coding 的仓库素材与评分入口仍按任务单独实现。

## 一次策略实验告诉了我们什么

框架从开发集失败证据提取问题，由模型提出策略假设，经人工核查后生成带版本的候选 Skill。候选冻结后，基线与候选在相同模型、案例、初始代码和容器环境下交替运行；留出任务在候选生成阶段不参与归因。

一次实验使用 `kimi-k2.7-code`，完成 **6 个任务 × 3 次重复 × 2 组 = 36 次完整运行**：

| 指标 | 无 Skill 基线 | 候选 Skill |
| --- | ---: | ---: |
| 开发集通过数 | 5/9 | 8/9 |
| 留出集通过数 | 8/9 | 6/9 |
| 总 Token | 362,212 | 633,813 |
| 工具调用次数 | 201 | 248 |

候选在已知的开发任务上提高了通过数，但在留出任务 H03 上从 2/3 降至 0/3，两次发生“同一次重复中基线通过、候选失败”。总 Token 增加约 75%，因此按预设回归规则 **拒绝候选**，没有把它加入默认 Agent。六个任务规模小、重复次数有限，这个结果展示的是可审查的策略决策，不代表大型仓库上的统计结论。

逐例数据见[实验报告](docs/coding-evolution-paired-trial-1-20261002.md)。原始 Trace、补丁和测试输出保留在本地 `eval/runs/`，未随源码上传；报告中指向这些文件的链接需本地证据包才能打开。最终文字回答的语义质量仍需人工复核。

## 快速开始

建议 Python 3.11；Coding 评测另需 Docker Desktop / Docker Engine 和本地镜像。

```bash
git clone https://github.com/yanxu18712287096-rgb/AgentEval.git
cd AgentEval
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

在 `.env` 中填写自己的模型名称、接口地址和密钥。代码当前保留 `corecoder` Python 包名及命令名，以兼容现有脚本。

先运行不调用真实模型的订单管线演练：

```bash
python -m corecoder.evaluation --cases eval/cases/examples.json --mode scripted --output eval/runs/orders-scripted-demo
python -m pytest -q
```

脚本演练只验证流程，不代表真实模型的通过率。每次评测使用新的输出目录。

运行 Coding 开发集真实模型基线（产生模型调用费用）：

```bash
docker pull python:3.11-slim
python -m corecoder.coding_eval --cases eval/coding_repository/exports/development-v1.1/cases.json --repeat 3 --output eval/runs/coding-dev-baseline
```

Docker 不可用时，Coding 评测会拒绝运行，不会改在宿主机执行命令。

## 场景与文档

- [评测使用说明](docs/evaluation-guide.md)：案例、Trace、评分、复核与指标。
- [Coding 任务集](eval/coding_repository/README.md)：3 个开发任务、3 个留出任务及需求—断言映射。
- [策略实验流程](docs/coding-strategy-evolution.md)：证据、假设、人工确认、候选生成和比较命令。
- [上下文压缩](docs/context-compression.md)：完整调用组、关键事实与失败回滚。
- [版本记录](docs/agent-upgrade-changelog.md)：各次改动、验证和限制。

## 当前边界

订单与 Coding 均为模拟场景，6 个 Coding 任务是小型合成仓库，不能代表大型真实仓库的普遍效果。历史留出集已完成分析，后续针对其修改策略时应作为已知回归集，并另备独立留出集。

Session 保存消息历史用于续聊，不恢复完整执行现场；当前没有独立的长期 Memory。上下文压缩已有机制测试，尚无真实模型长对话对照收益结论。

## License

[MIT](LICENSE)。
