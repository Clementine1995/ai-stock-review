# 项目文档索引

## 职责

本文件说明每份项目文档负责回答什么问题、哪些内容具有当前约束力，以及目标合同、实现事实和历史材料如何区分。文档之间不复制大段命令或规则；需要修改某项口径时，只修改它的责任文档，并更新引用。

## 权威范围

| 文档 | 唯一职责 | 地位 |
| --- | --- | --- |
| [`../README.md`](../README.md) | 项目定位、当前已知状态、最快入口和文件地图 | 当前事实入口；状态变化时同步 |
| [`PRODUCT_REQUIREMENTS.md`](PRODUCT_REQUIREMENTS.md) | 产品目标、正式对象、行为合同和验收标准 | 当前定版产品合同，不自动证明已经实现 |
| [`../stock-review.md`](../stock-review.md) | STEP 1-10 业务框架和用户交易纪律 | 当前业务框架来源 |
| [`../AGENTS.md`](../AGENTS.md) | AI 读取顺序、授权边界和最高优先级禁令 | 协作入口 |
| [`../PROJECT_RULES.md`](../PROJECT_RULES.md) | 项目特有且可验证的架构、数据和状态红线 | 工程红线来源 |
| [`../STYLE.md`](../STYLE.md) | 代码、注释、命名、测试和字符集规范 | 工程风格来源 |
| [`../AI_COLLABORATION.md`](../AI_COLLABORATION.md) | 任务分级、开工、执行、证据和收口 | AI 执行流程来源 |
| [`../DEV_WORKFLOW.md`](../DEV_WORKFLOW.md) | 本地环境、测试、Git 与数据库的人工作业命令 | 开发操作命令的唯一来源 |
| [`../REQUIREMENT_DEVELOPMENT.md`](../REQUIREMENT_DEVELOPMENT.md) | 单项需求合同的触发条件、结构和状态 | 复杂新需求的维护规则 |
| [`../integrations/workbuddy/stock-review/SKILL.md`](../integrations/workbuddy/stock-review/SKILL.md) | WorkBuddy 对当前 CLI 的运行时编排协议 | 适配合同；真实验收尚未完成 |
| [`LOCAL_DATA_OPERATIONS.md`](LOCAL_DATA_OPERATIONS.md) | 新旧数据库、证据、报告、日志、备份与清理边界 | 当前数据操作说明 |
| [`REVIEW_QUALITY_REDESIGN.md`](REVIEW_QUALITY_REDESIGN.md) | 2026-09-05 重构前后的审计与设计过程 | 冻结历史快照，非当前规范 |
| [`../PRD.md`](../PRD.md) | 上一版失败项目材料 | 历史参考，非当前合同 |

[`../integrations/workbuddy/README.md`](../integrations/workbuddy/README.md) 是 WorkBuddy 接入说明，负责把运行时协议与当前项目状态连接起来；具体编排规则仍由 Skill 维护。

## 事实判定

- “需求已写入”只说明目标合同存在。
- “代码已实现”需要代码位置和匹配自动化测试共同支持。
- “真实数据源可用”“真实模型已调用”“数据库已迁移”“WorkBuddy 已验收”必须有对应环境、资源身份、时间、输入、输出和只读回查证据。
- Mock、临时 SQLite、离线样例和本地单元测试只能证明受控场景，不得升级成真实运行结论。
- README 的当前状态应与代码和验证证据同步；发现冲突时先按只读核查确认事实，再修正文档。

## 内容归属

- 产品对象、状态、输入输出和验收规则进入 `PRODUCT_REQUIREMENTS.md`。
- STEP 顺序、交易框架和用户判断纪律进入 `stock-review.md`。
- 架构禁止事项及其验证映射进入 `PROJECT_RULES.md`。
- 命令、工作目录、前置条件、影响和成功标准进入 `DEV_WORKFLOW.md`。
- 跨模块、状态机、接口、数据库或分批交付的新需求在 `docs/requirements/` 建立单项合同。
- 当前实现状态和用户最快入口只在 README 摘要，不在多份文档维护平行状态表。
- 临时调查、对话纪要、一次性日志和截图默认不进入仓库。

## 更新规则

1. 修改责任文档，再检查所有引用它的入口。
2. 行为合同变化时同步代码、测试、产品或单项需求文档；只改说明文字不能证明行为变化已经完成。
3. 可复制执行的开发、测试、Git 和数据库人工作业流程只在 `DEV_WORKFLOW.md` 更新；CLI 的行为签名属于产品合同，WorkBuddy 的机器编排参数属于 Skill。三者变化时同步核对，README 与接入说明只保留入口名称和链接。
4. 历史材料保持冻结。旧结论过时时添加醒目标记，不逐段修成当前规范。
5. 文档收口时检查相对链接、模板占位符、旧库/新库口径、WorkBuddy 验收状态和 `git diff --check`。
