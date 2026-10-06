# v0.16.0 面试展示版：Coding Agent 评测闭环

此版是可学习、可演示的稳定交付范围，不是生产级沙箱或大型仓库 benchmark。保留订单客服等业务场景，新增代码产物场景；工程主线是“隔离执行 → 完整证据 → 客观评分 → 版本回归 → 评分器错误可重审”。第一被测对象是本项目自身的 CoreCoder Agent，不冒称可直接评测任意外部 Agent。

## 已实现的执行路径

1. [8 条微型 Python 修复案例](../eval/coding/cases.json)各包含初始文件、用户 issue、允许改动范围、公开测试和模型不可见的隐藏测试；4 条 development、4 条原设计 holdout。每次重复从同一 fixture 新建临时工作区，不在案例间传递文件状态。
2. Agent 主循环和模型 API 请求运行在宿主 Python 进程；**文件工具**受工作区路径限制，**Shell 和评测测试**在 Docker 容器运行。容器禁网、只读挂载、非特权用户、去除 capabilities，并限制进程、内存和 CPU。模型 API 凭证不传入容器。Docker Engine 或本地 Python 镜像不可用时拒绝运行，不回退宿主 Shell。这不等于把整个 Agent 放进容器，也不是恶意代码的生产级安全保证。
3. 执行后保存完整 JSONL trace、最终文件内容与哈希、公开/隐藏测试退出码和输出、运行指纹、模型/工具轮次、token 和耗时。模型可见历史压缩不替代 trace。工具请求与结果按调用键、名称、ID、Agent/轮次及顺序配对。
4. 任务客观结果由执行完成性、公开/隐藏测试、改动范围和 trace 协议决定。可恢复的工具错误单独统计为效率告警，不把“过程不完美”误写成“任务失败”。回答文本仍标记 `answer_review_pending`，没有自动 LLM Judge 或完成语义人工裁定。
5. `--resume` 核对运行指纹、产物哈希和完整 trace；`--baseline` 对指纹不同的运行拒绝数值直比；历史补丁重评分使用独立 `corecoder.coding_rescore`，重新执行当前测试但不再次请求模型，明确保留原报告和来源哈希。

## 验证与面试口径

- `RUN_DOCKER_CODING_TESTS=1 venv311/bin/python -m pytest -q`：492 passed。
- `kimi-k2.7-code` 对 8 条案例各运行 3 次，原始评分 21/24；审计发现 2 次隐藏断言超出 issue、1 次把已恢复工具错误误判任务失败。保留原始证据，修正后对历史补丁重测为 24/24 客观通过，未再次调用模型。详见 [逐项审计](coding-evaluation-live-review-v0.16.0.md)。
- 这证明本项目可以发现并修复评测器自身错误；**不证明**模型在复杂 coding 任务上 100% 成功。案例都是小型合成缺陷，当前 holdout 在审计中被查看，不能再作为后续调参的盲测。面试时应主动说清楚这一限制。

## 刻意不纳入本版

跨设备路由、Electron、多聊天渠道、异步 Branch Agent、跨进程 coding checkpoint、自动 Skill 自进化、通用外部 Agent 适配器、自动 LLM Judge 与大型仓库任务基准均未实现。这些是独立的研究/产品问题。若面试重点转向大任务泛化，应另建未查看的真实仓库案例和更强隔离，不复用这 8 条微型案例冒充大型任务证据。

参考 XiaoBa-CLI 的 [稳定工具边界后 checkpoint](https://github.com/buildsense-ai/XiaoBa-CLI/blob/main/docs/checkpoint-compaction-v2.md)及 [隔离分支会话](https://github.com/buildsense-ai/XiaoBa-CLI/blob/main/docs/branch-session-architecture.md)设计思想；本版没有复制这些未实现能力。
