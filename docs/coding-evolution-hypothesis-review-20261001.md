# Coding 策略假设待审（2026-10-01）

状态：用户已在 2026-10-01 的对话中确认归因，确认凭据已封存；候选 Skill 已生成并完成成对回归。结果为[拒绝 trial-1](coding-evolution-paired-trial-1-20261002.md)，没有激活。

## 证据

- 开发集 D03 的第 1、3 次执行客观测试失败；两次均通过公开测试，但未通过 R1 的失败状态不变检查。[E1 Trace](../eval/runs/repository-dev-calibration-20261001/REPO-D03-00/trace.jsonl)、[E2 Trace](../eval/runs/repository-dev-calibration-20261001/REPO-D03-02/trace.jsonl)。
- [公开需求 R1](../eval/coding_repository/development/inventory-reservation/task.json) 明确规定：任一 SKU 不存在或不足时抛错，库存和预留记录保持调用前状态。[原始 `Stock.take`](../eval/coding_repository/development/inventory-reservation/repo/inventory/stock.py) 在循环中逐项检查并立即扣减；后项失败会留下前项扣减。
- 两次 Agent 修改了 `service.py` 与 `ledger.py`，没有修改 `stock.py`。公开测试仅检查成功预留和释放，[开发集验收器](../eval/coding_repository/development/inventory-reservation/verify.py) 进一步检查后项失败时的状态不变。

## 模型归因与可检验假设

模型归因：Agent 漏掉了批量库存扣减的原子性缺陷。候选策略应提醒 Agent：遇到一次操作更新多个资源且需求要求全有或全无时，先追踪实际状态变更链，再检查中途失败的状态；修复应覆盖所有受影响的状态变更位置，并用失败路径测试验证调用前后状态一致。

这只是策略假设。模型还提到可能是快照隔离、异常类型或释放重试等其他路径；开发集的逐项验收结果和原始代码目前更支持原子性归因。假设的适用边界是有状态、多资源、失败回滚或幂等要求的任务，不应强行施加于所有 Coding 任务。

冻结的[模型原文及证据哈希](../eval/runs/repository-dev-baseline-rescore-v3-20261001/hypothesis.json)保留完整字段。用户确认后，才执行 `confirm`、模型候选 Skill 生成、3 次重复的开发集和留出集成对回归。比较客观通过数、回归项、token、模型轮次、工具调用和耗时；若候选引入原本成功任务的失败，则拒绝。最终回答仍需人工复查。
