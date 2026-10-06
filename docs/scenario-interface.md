# 0.10.0 场景接口与历史重评分

本次实现第一批基础设施，不开展 Skill 效果实验，不增加新的业务应用。默认只有 `orders@1`，Role 为 `order-support@1`，Skill 列表为空。原有 40 条案例全部为 development。

## 接口边界

定义见 `corecoder/scenarios.py`。采用进程内显式注册，不扫描或执行外部插件。

| 接口 | 内容 | 边界 |
| --- | --- | --- |
| RoleSpec | ID、版本、提示词、工具白名单、显式 Skill 列表 | 白名单约束实际工具工厂；不是只写提示词 |
| SkillSpec | ID、版本、正文、兼容 Role ID | 仅提供契约和显式拼接入口，无自动选择/检索；默认不加载 |
| CaseSpec | ID、split、用户回合、fixture、expect、route、scenario、scenario_version | 防御性复制；expect 由对应场景验证，不要求退款字段 |
| ScenarioSpec | Role、状态/工具工厂、案例校验、评分、状态快照/恢复、评分版本、实现文件 | 工厂必须每次返回独立实例；快照必须 JSON 可序列化 |
| ScenarioRegistry | 显式注册和版本解析 | 拒绝重复注册、未知场景/版本 |

新增场景时实现工厂及业务评分回调，注册 `ScenarioSpec`，通过 `run_case(..., registry=registry)` 或 `validate_cases(..., registry=registry)` 使用。CLI 当前使用 `default_registry()` 中的显式注册；不支持加载任意路径的 Python 配置。

评分回调 `scorer(case, events, state)` 返回字典，必需字段是 `failures: list[str]`；可选字段为 `failure_groups`（business/safety/evidence）、`route_failures`、`optimal_route`、`tool_results` 和业务指标。通用评分层追加协议完整性、执行终态和预算检查。业务错误不应依靠通用层猜测含义；未分组错误归为 business。

`snapshot(state)` 只保存评分必需的业务终态；`restore(snapshot)` 构造离线评分状态，不调用外部系统。它不是可恢复执行的完整 checkpoint。`implementation_files` 声明额外需要纳入指纹的代码路径（相对于 corecoder，或绝对路径）；内置 Python 源文件均自动计算哈希。

订单断言已独立到 `corecoder/order_evaluation.py`。Agent 主循环不因场景迁移而修改。旧案例没有场景字段时，兼容适配为 `orders@1`；新案例应显式填写。现有 JSON 已增加这两个字段，业务输入、断言、预算保持不变。

## 分维度评分

- execution：请求/结果一一对应、名称与调用身份一致、先后顺序、回合完成、运行异常。
- business：场景目标与副作用是否符合预期。
- safety：场景禁止操作/内容、退款结果未知时盲目重试等。
- evidence：订单场景的退款前证据、回答断言、成功声明时序。
- efficiency：超预算及可避免的拒绝调用。

维度表示已有检查的结果，不意味着全面语义验证；没有触发检查不等于已证明所有风险不存在。评分器仍使用结构化断言和有限短语规则，不含 LLM judge。总通过率保留原定义，高效路线还要求预算与路线检查通过。

## 三种不同操作

```bash
# 重新执行现有案例（scripted 仅验证链路）
venv311/bin/python -m corecoder.evaluation --cases eval/cases/examples.json --mode scripted --repeat 3 --output eval/runs/my-new-run

# 只重评分一个已经完成的历史 trace，无模型/工具调用
venv311/bin/python -m corecoder.evaluation --rescore eval/runs/v0.9.4-scripted-verified/000-00.jsonl --output eval/rescores/my-review

# 提取历史案例，重新运行当前 runtime；--replay 是保留的兼容别名
venv311/bin/python -m corecoder.evaluation --rerun eval/runs/v0.9.4-scripted-verified/000-00.jsonl --mode scripted --output eval/runs/my-rerun
```

输出目录必须不存在，防止覆盖原始证据。真实模式需显式 `--mode live`，会重新调用模型。

`--rescore` 保存 source trace 路径/哈希、源运行版本/模式、当前评分版本/哈希和新结果；不修改旧文件，不伪造新 token/耗时。只接受完整、序号连续、run_id 一致且案例哈希匹配的 trace。支持 schema 2/3/4 的已知封装，但历史案例仍须通过当前案例校验；旧 `final_answer_contains` 断言不被静默迁移。不兼容时应保留旧评分或另行设计审核后的迁移。

新 trace 用通用 `state` 保存业务评分终态；旧订单 trace 的退款/工单计数有显式兼容读取。仅重评分历史已发生的轨迹，不能补出当时缺失的日期、工具结果或重新做决策。

严格录制响应回放、任意步骤恢复、自动迁移历史断言均未实现；本次 `--rerun` 不是这些能力。

## 指纹与比较

schema 4 的每次运行记录：包版本、Python 源文件哈希、场景/评分版本、Role/Skill 内容与哈希、工具 schema 哈希、案例哈希、模型与调用配置。CLI 记录 endpoint 的哈希而非 API key；不在指纹中存密钥。

`--baseline` 只对指纹完全一致的同案例运行给出直接对比。缺失指纹或版本/内容/参数发生变化时，返回 `not_comparable` 和具体原因。此规则刻意保守；未来受控的 Skill 对照实验需要增加“允许变化项”协议，而不是忽略这些差异。

续跑同样校验指纹，拒绝将旧代码产生的结果混入新版本运行。历史记录保留，不强行回填未知元数据。指纹用于可追溯性，不是防篡改签名；trace 仍是本地文件，需要自行管理访问权限和敏感内容。
