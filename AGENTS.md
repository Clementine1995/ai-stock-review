# AGENTS.md

## 职责

本文件是 AI 进入仓库后的约束入口，只保存文档读取顺序、授权边界和最高优先级的协作禁令。产品合同、项目红线、工程风格和可执行命令分别由对应文档维护，不在这里重复。

## 必读顺序

开始任务前按顺序读取：

1. [`README.md`](README.md)：项目定位、当前事实、最快入口和文件地图。
2. 本文件：授权与禁止事项。
3. [`PROJECT_RULES.md`](PROJECT_RULES.md)：项目特有、可验证的架构和数据完整性红线。
4. [`STYLE.md`](STYLE.md)：代码、注释、命名、测试和字符集规范。
5. [`AI_COLLABORATION.md`](AI_COLLABORATION.md)：任务分级、开工、执行、验证和收口流程。

按任务需要再读取：

- [`DEV_WORKFLOW.md`](DEV_WORKFLOW.md)：本地环境、测试、Git 和数据库人工作业命令的唯一来源。
- [`REQUIREMENT_DEVELOPMENT.md`](REQUIREMENT_DEVELOPMENT.md)：需要长期维护的单项需求合同。
- [`docs/README.md`](docs/README.md)：项目文档职责、权威范围和历史状态。
- [`integrations/workbuddy/stock-review/SKILL.md`](integrations/workbuddy/stock-review/SKILL.md)：WorkBuddy 的运行时编排合同。

## 文档与事实边界

- 用户在当前会话中的明确要求决定本次任务目标和授权范围。文档与用户要求冲突时必须指出冲突，不得静默扩大操作。
- [`docs/PRODUCT_REQUIREMENTS.md`](docs/PRODUCT_REQUIREMENTS.md) 定义产品目标合同；[`stock-review.md`](stock-review.md) 定义 STEP 1-10 业务框架和用户纪律。
- 需求文档描述目标，不自动证明功能已经实现。当前实现事实由代码、匹配测试、README 当前状态和执行前只读核查共同证明。
- 稳定的业务合同、架构决策和验收记录可以进入仓库；聊天流水、临时推理、截图、日志副本和一次性协作材料默认不进入仓库。
- 根目录 `PRD.md` 和 [`docs/REVIEW_QUALITY_REDESIGN.md`](docs/REVIEW_QUALITY_REDESIGN.md) 是历史材料，不能定义当前入口、命令或验收结论。

## 默认允许

- 读取、搜索和比较仓库文件。
- 运行 `git status`、`git diff`、`git log` 等只读 Git 命令。
- 查看配置、日志、JSON、CSV、Markdown 和源码。
- 在已经授权的小改动中，运行不访问真实网络、不写正式业务库的本地匹配测试。
- 在用户已经要求修改时，编辑与当前目标直接相关的代码、测试或文档。
- 执行数据库普通 `SELECT` 前，必须先说明实际环境、数据库绝对路径和影响范围；事务内不得包含 DDL、DML、迁移或其它状态变更。

## 必须单独取得明确授权

以下授权互不替代，也不能从一般“继续”“运行测试”或“做一次复盘”中推定：

- `git fetch`、`git pull`、`git add`、`git commit`、`git push`，以及其它远端同步、分支合并或发布。
- 删除任何文件或目录；移动、清空、覆盖历史数据、日志、配置、报告或其它状态性内容。
- 安装、升级、降级或移除依赖，持久修改环境变量、`.env` 或业务运行配置。`DEV_WORKFLOW.md` 中仅对当前测试进程生效的隔离变量不属于持久配置修改。
- 调用真实数据源、把冻结请求交给正式模型，或执行真实 WorkBuddy 端到端。来源、对象、模型和影响必须受当前明确请求及获确认 RunManifest 约束；最终提交仍需要单独确认 FinalizationProposal。
- 数据库写入、数据修复、软删除、DDL、迁移和恢复。
- 调用真实写接口、改变远端服务状态或执行生产/远端操作。

数据库迁移还必须遵守 [`DEV_WORKFLOW.md`](DEV_WORKFLOW.md#数据库迁移) 的 MigrationPlan、`plan_hash`、SQLite Online Backup 和逐次确认协议。一般本地写授权、测试授权、WorkBuddy 授权或一次复盘授权不能替代迁移授权。

## 最高优先级禁令

- 始终直接使用用户现有的 `F:\codex-project\ai-stock-review` 工作目录；除非用户在当前请求中明确要求，不创建新 workspace 或 Git worktree。
- AI 不得使用 `git reset`、`git reset --hard`、`git checkout <commit> -- <path>`、`git restore`、`git revert` 或脚本化等价物修复流程错误。发生错误时停止，报告分支、HEAD、工作区状态、已执行命令和人工恢复选项。
- 不改与当前目标无关的代码、格式、命名、依赖、配置、数据或文档；发现旁支只记录。
- 不以少改代码为理由保留已经明确废弃的双轨、fallback、临时映射或隐藏入口。当前旧 CLI 和 `scripts/run_daily_review.ps1` 的保留期是 [`PROJECT_RULES.md`](PROJECT_RULES.md) 明确规定的例外，验收完成前不得删除。
- 真实模型、数据源、SQLite 写入或 WorkBuddy 调用超时且结果不明时，不得重复执行。先只读判断未执行、执行中断、已生效但响应丢失或部分生效，再决定后续动作。
- 不在仓库、命令输出、日志或对话中写入密钥和完整敏感配置。
- 当前仓库没有已定义的 CI/CD、测试环境或生产部署流程。不得虚构环境、分支、服务、健康检查或发布证据。

## 执行要求

- 非平凡任务必须先按 [`AI_COLLABORATION.md`](AI_COLLABORATION.md#开工卡) 给出目标、不做范围、完成标准和验证方式。
- 写文件前说明将修改的文件和每个文件的目标；只沿当前目标的直接调用链修改。
- 开始改动前只读确认当前工作区、分支、HEAD、已有差异和目标文件重叠情况；已有用户改动必须保留。
- 优先完成当前目标的最小闭环。达到完成标准后立即收口，不继续深挖体验增强、架构优化或清理项。
- 自动化测试必须使用临时数据库、受控替身或离线样例；不得把 Mock 或本地测试写成联网、真实模型、旧库迁移或正式业务运行证据。
- 结论必须区分已验证事实、基于证据的推断和未验证风险；文件与实现事实尽量给出路径和行号。

## 多智能体协作

- 可以把边界清晰、相互独立的只读核查或文件修改交给子智能体，但不得因此扩大用户授权。
- 主智能体负责共享工作区的写入规划、冲突处理、最终集成和验证；同一文件不得交给多个智能体并发修改。
- 子智能体发现跨范围问题时只报告，不自行扩展任务、执行真实写入或进行 Git 写操作。

## 字符集

中文文档与源码文本统一使用 UTF-8。读取和写入中文文件时必须显式保持正确编码，发现乱码立即停止。
