# 多文件仓库维护任务集

当前版本：`repository-maintenance-v1.1`（2026-10-01）。3 个开发任务 + 3 个留出任务。

这是可执行的小型合成仓库任务集，不是从真实开源项目历史 issue 提取的大型仓库基准。
每个仓库有 5–6 个 Python 文件、约 45–84 行 Python（含公开测试），导出时增加 `TASK.md`。
比原单函数题多了调用方、状态、存储/缓存和端到端不变量，但规模仍小；不能以文件数量宣称生产级难度。
每个参考修复改动两个实现文件，测试不要求 Agent 使用参考补丁或恰好修改两个文件。

## 使用哪些文件

- `exports/development-v1.1/cases.json`：仅开发集，先用它做真实模型基线和失败分析。
- `exports/all-v1.1/cases.json`：冻结候选后的完整新旧策略回归，包含开发集及留出集。
- `exports/holdout-v1.1/cases.json`：留出集单独导出，不用于策略生成或日常调参。
- `suite-lock.json`：当前源文件哈希；源任务/断言/参考修复变化后，导出拒绝继续使用旧指纹。
- `audit-docker-v1.1.json`：原始代码、参考修复、逐文件撤回修复的完整容器审计输出。
- `*/<task>/task.json`：公开任务要求与要求—测试方法映射。
- `*/<task>/repo/`：Agent 可见的初始代码。
- `*/<task>/verify.py`、`reference.json`：仅供验证与人工审计，**不进入 Agent 工作区**。

早期 `exports/*-v1`、`suite-lock.preflight-v1.json`、`audit-docker-v1.json` 保留初次审计证据，不用于当前实验。
v1.1 将两条直接访问内部存储属性的断言改为公开接口断言，避免限制等价正确实现。

## 开发任务

| ID | 场景 | 需要保持的行为 |
|---|---|---|
| REPO-D01 | 文件化任务队列 | 领取时计数、租约恢复、次数上限、恢复持久化与重复调用 |
| REPO-D02 | 客户端配置链路 | 分层优先级、显式 False/0/空串、环境解析、调用方传播 |
| REPO-D03 | 库存预留 | 批量操作原子性、幂等冲突、释放一次、快照隔离 |

留出任务使用不同代码库，覆盖分页边界、授权与修订一致性、跨时区区间及改期；不是开发题改变量名。
适用的是“未参与策略生成的留出集”，不是“作者未看过”或“独立第三方盲测”。
编写和审计阶段已查看全部任务；首次实验后不得根据留出失败持续修改同一候选并仍称独立验证。

## 需求与断言如何对齐

每个任务的 R1–R4 都出现在用户可见 issue 和 `TASK.md` 中；验收恰好映射到四个 `test_R*` 方法。
隐藏的是输入组合与测试实现，不是行为要求。AST 检查保证没有未映射的测试方法；人工逐条核对语义。

| 任务 | R1 | R2 | R3 | R4 |
|---|---|---|---|---|
| D01 | attempts 更新时点 | 到期边界与重试上限 | 磁盘恢复与幂等 | 重复领取与独立快照 |
| D02 | 各层显式值优先级 | 公开规定的字符格式 | 输入不变与调用方一致 | 默认请求回归 |
| D03 | 不足/未知 SKU 无部分写入 | 重试与内容冲突 | 释放一次与释放后重试 | 输入输出快照 |
| H01 | 复合键与租户顺序 | 下一页/终止条件 | 游标契约与异常 | 输出快照 |
| H02 | 每次请求鉴权 | 修订与最新内容 | 输入缓存隔离 | 批量读取授权 |
| H03 | UTC 与时间校验 | 半开区间冲突 | 改期原子性 | 取消与排序 |

检查中已显式补充 ASCII 数字/空白限制、decode 的二元组返回类型、UTC 与相接边界；
这些都在公开需求中，不从隐藏测试反向要求 Agent 猜测。
测试只执行标准库，不依赖外网、数据库、真实时钟或第三方服务。

## 审计门槛

2026-10-01 验证结果：Docker 下 6/6 原始仓库通过公开检查且被验收检出缺陷；6/6 参考修复通过全部检查；12/12 不完整修复被检出；24 项要求与验收方法映射一致。完整命令 `RUN_DOCKER_CODING_TESTS=1 venv311/bin/python -m pytest -q` 为 **520 passed**。封存源文件与审计报告哈希绑定复核通过。未调用真实 LLM。

冻结前同时满足：

1. 原始代码通过公开既有功能检查。
2. 原始代码至少违反一项新需求验收。
3. 参考修复通过公开和全部验收。
4. 逐个撤回参考修复中的文件改动后，验收检出不完整修复。
5. 开发/留出导出身份一致、模型工作区不含参考修复和验收器。

这证明参考解可执行、已知缺陷可检出，不证明测试穷尽所有可能错误。
单文件撤回是有限变异测试，不等于完整 mutation coverage，也不是 LLM 失败证据。

## 真实模型基线与后续命令

开发集 3×3 次 `kimi-k2.7-code` 真实模型基线已完成；原评分 6/9，对保存补丁统一复验后客观结果 7/9，两次真实失败均为 D03 批量扣库存非原子。逐例证据与成本见 [基线复核](../../docs/repository-coding-live-baseline-20261001.md)。留出集尚未运行。

以下是复现实验入口；新运行须使用新的输出目录：

```bash
venv311/bin/python -m corecoder.coding_eval --cases eval/coding_repository/exports/development-v1.1/cases.json --output eval/runs/repository-dev-baseline --repeat 3
venv311/bin/python -m corecoder.coding_evolution collect --cases eval/coding_repository/exports/development-v1.1/cases.json --input eval/runs/repository-dev-baseline/scorecard.json --output eval/runs/repository-evidence.json
```

当前可审查的失败证据在 `eval/runs/repository-dev-baseline-rescore-v3-20261001/evidence.json`。之后按 [策略闭环说明](../../docs/coding-strategy-evolution.md) 进行假设生成、人工确认和候选冻结。
完整回归使用 `exports/all-v1.1/cases.json`，它与开发集导出的开发案例指纹一致。
若开发集依然稳定全通过，不制造失败、不宣称自进化收益；先判断成本问题或引入新的真实失败模式。

重新审计可以使用新的输出路径：

```bash
venv311/bin/python -m scripts.repository_suite audit --docker --output /tmp/repository-audit-new.json
venv311/bin/python -m scripts.repository_suite build --split development --output /tmp/repository-development-export
```

`freeze` 仅用于新版本首次封存，不覆盖已有锁。新版本应保留旧导出/审计并重新通过门槛。
