# AgentEval｜多场景 Agent 运行与可追溯评测回归框架

AgentEval 将任务执行、证据记录、自动评分与策略回归串成一条可复查的流程。目标是回答：Agent 是否真正完成任务？失败发生在哪一步？注入新的 Skill 后，是整体改善，还是只提高了已知任务的分数？

技术栈：Python、Function Calling、JSONL Trace、pytest / unittest、Docker、MCP stdio；可选 LiteLLM 模型适配。

## 核心能力

- **执行层**：复用 Runtime，按 Role 配置任务身份与工具，按需注入带版本的 Skill；支持会话保存、工具调用与结果配对、分层上下文压缩。
- **证据层**：记录用户输入、模型响应、工具请求与结果、异常、Token usage 和耗时；Trace 独立于压缩后的消息历史。
- **评测层**：38 条活动订单案例检查业务状态、授权与前置证据；6 个多文件 Coding 任务检查补丁范围、公开测试、隐藏测试和执行完整性。
- **回归层**：冻结案例、初始代码、模型配置和策略，核对版本指纹与产物哈希，区分任务完成度、效率和成本。
- **策略实验**：开发集失败证据 → LLM 提出假设 → 人工确认 → 生成候选 Skill → 冻结 → 成对回归 → 接受或拒绝。不会自动激活候选。

## 已完成的真实模型实验

固定 `kimi-k2.7-code`、Runtime、案例和容器环境，仅改变候选 Skill，完成 **6 个任务 × 3 次重复 × 2 组 = 36 次完整运行**。

| 指标 | 无 Skill 基线 | 候选 Skill |
| --- | ---: | ---: |
| 开发集通过数 | 5/9 | 8/9 |
| 留出集通过数 | 8/9 | 6/9 |
| 总 Token | 362,212 | 633,813 |
| 工具调用次数 | 201 | 248 |

候选出现两次“基线通过、候选失败”的配对回归，总 Token 增加约 75%，最终 **拒绝候选**。这是发现策略退化的实验结果，不是自进化成功或统计显著性的证明。

详见 [实验报告](docs/coding-evolution-paired-trial-1-20261002.md)。原始运行产物保留在本地 `eval/runs/`，未随源码上传；历史报告中指向该目录的链接需本地证据包才能打开。最终回答的语义质量仍需人工复核。

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

在 `.env` 中填写自己的模型名称、接口地址和密钥。仓库保留 `corecoder` Python 包名及命令名，以兼容现有脚本；GitHub 项目名为 AgentEval。

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

Coding 终端命令与测试在受限容器执行；文件工具限制在单案例临时工作区。Docker 不可用时拒绝运行，不回退宿主机。普通交互 CLI 和 MCP 服务并不自动受到同样的容器隔离。

## 场景与文档

- [评测使用说明](docs/evaluation-guide.md)：案例、Trace、评分、复核与指标。
- [Coding 任务集](eval/coding_repository/README.md)：3 个开发任务、3 个留出任务及需求—断言映射。
- [策略实验流程](docs/coding-strategy-evolution.md)：证据、假设、人工确认、候选生成和比较命令。
- [上下文压缩](docs/context-compression.md)：完整调用组、关键事实与失败回滚。
- [版本记录](docs/agent-upgrade-changelog.md)：各次改动、验证和限制。

## 当前边界

订单与 Coding 均为模拟场景，6 个 Coding 任务是小型合成仓库，不能代表大型真实仓库的普遍效果。历史留出集已完成分析，后续针对其修改策略时应作为已知回归集，并另备独立留出集。

Role / Skill 复用公共描述，场景工具和评分逻辑按任务实现；Coding 仍有独立评测入口。Session 用于续聊，不恢复完整执行现场；未实现独立长期 Memory。上下文压缩已有机制测试，尚无真实模型长对话对照收益结论。

## License

[MIT](LICENSE)。
