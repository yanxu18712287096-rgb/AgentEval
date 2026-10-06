# 本项目的更新记录约定

用户要求每次在原 Agent 基础上更新时，都在文档中说明版本、改动和相对上一版的升级。

- 修改 Agent 能力、评测器、场景规则或案例集时，同步更新 `docs/agent-upgrade-changelog.md`，记录前后差异、理由、验证证据和已知限制。
- 发布新的本地功能版本时，保持 `pyproject.toml` 与 `corecoder/__init__.py` 版本一致。尚未完成的实验和纯文档修改记录到 Unreleased，不冒充已发布功能。
- 根据行为变化同步维护 `docs/evaluation-guide.md`、`docs/agent-upgrade-plan.md` 或案例说明；保留历史版本记录。
- 明确区分 CoreCoder 原有能力、本地已有改动和本次新增能力。离线 scripted 演练不得表述为真实模型效果。
