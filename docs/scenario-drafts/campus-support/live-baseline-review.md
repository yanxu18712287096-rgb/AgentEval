# 校园助手无 Skill 真实模型基线：逐例复核

日期：2026-09-30。配置：`kimi-k2.7-code`，OpenAI 兼容接口，temperature 1，max_tokens 4096；12 条 development，每条 1 次；Role `campus-usage-assistant@1`，Skill 列表为空。API 密钥不记录。

运行目录：[v0.12.0-campus-kimi-live-baseline-network](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/)，包含 [自动报告](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/report.md)、[结构化分数](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/scorecard.json)及 `000-00.jsonl` 至 `011-00.jsonl` trace。

## 原始自动结果

12/12 完整结束，模型用量 12/12 可用；规则层 12/12 通过，综合 0 PASS / 12 REVIEW / 0 FAIL。所有语义项目前都进入 REVIEW，因此不能把 12 条规则通过解释为 100% 业务通过。程序退出码 2 表示待复核。共 28 模型轮次、21 工具调用；工具结果 19 成功、2 次预设超时；token 25,111（输入 19,473、输出 5,638）；逐例耗时之和 524.679 秒，平均 43.723 秒。耗时是本机执行加网络等待，非纯模型推理时间。

## 助手辅助语义复核（暂定，不覆盖原始标签）

依据 [案例标准](../../../eval/cases/campus-support-draft.md) 比对用户输入、工具返回和每轮最终回答。此复核者参与过案例编写，不是独立盲审；用户可按 trace 逐条改判。暂定 **10 PASS / 2 FAIL**，业务通过 10/12（83.3%）；每条仅一次，不能代表稳定通过概率。

| 案例 | trace | 暂定 | 判断依据 |
| --- | --- | --- | --- |
| CS-D01 | [000](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/000-00.jsonl) | PASS | 正确解释 28 MiB 超限并建议压缩；额外公告查询不改变结论，作为效率观察。 |
| CS-D02 | [001](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/001-00.jsonl) | PASS | 正确处理等于 20,971,520 字节的边界，没有保证实际上传成功。 |
| CS-D03 | [002](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/002-00.jsonl) | PASS | 询问报错、格式、大小；没有提前确定原因。 |
| CS-D04 | [003](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/003-00.jsonl) | PASS | 区分通常 2 分钟与 10 分钟核查阈值，询问实际等待时长；回答偏长。 |
| CS-D05 | [004](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/004-00.jsonl) | PASS | 恰好三个编号步骤，入口、日期、导出 PDF 齐全。 |
| CS-D06 | [005](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/005-00.jsonl) | **FAIL** | 表格内容正确且“未说明”正确，但在表格后另加“依据”段落，违反用户明确的“只用表格”，触发 CS-D06-T1-R1 的输出格式要求。 |
| CS-D07 | [006](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/006-00.jsonl) | **FAIL** | 09:42 只能看到 09:10 的调查公告。回答“确实存在上传服务故障，目前尚未恢复”“恢复状态：未恢复”，把尚无恢复确认误写成确定未恢复；实际服务 09:40 已恢复但 09:45 才公告。违反 CS-D07-T1-R2/F2 的知识边界。没有泄露未来 U2，工具时间过滤正确。 |
| CS-D08 | [007](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/007-00.jsonl) | PASS | 09:15 有相关公告，区分 09:45 发布和 09:40 恢复；额外当前查询返回空，不影响历史判断。 |
| CS-D09 | [008](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/008-00.jsonl) | PASS | 第一轮澄清，第二轮跟随新意图仅回答支持格式；复用已取得资料。 |
| CS-D10 | [009](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/009-00.jsonl) | PASS | 第一轮处理超限，第二轮依据新报错查询 U1 并改变判断。末尾再问是否需要查“最新公告”略重复，但不影响任务完成。 |
| CS-D11 | [010](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/010-00.jsonl) | PASS | 两次查询均超时，回答明确无法确认公告存在与否，建议稍后重试；后续排查问题较多，条件式“工具恢复我会查询”不作为自动监控能力承诺计分。 |
| CS-D12 | [011](../../../eval/runs/v0.12.0-campus-kimi-live-baseline-network/011-00.jsonl) | PASS | 成功空结果与超时区分，说明没有公告不能保证能登录。 |

## 结果边界与后续

- 这是开发集上的单次运行。两项失败分别属于格式遵循与证据边界，不应通过更改案例标准或放宽评分抹掉。
- 规则层目前无法自动识别这两项语义失败；它们验证了保留 REVIEW 与人工复核的必要性。后续若接 LLM Judge，应使用这两项及正确、模糊对照回答校准，保留 Judge 原始输出和人工改判。
- D01、D04、D08 等存在额外查询或长回答，效率指标应单独分析，不与业务通过混合。
- 第一次沙箱网络请求在首例出现 `APIConnectionError`，产物位于 `eval/runs/v0.12.0-campus-kimi-live-baseline/`，未纳入本次分母。完整运行采用网络权限，未因 provider 错误中断。
