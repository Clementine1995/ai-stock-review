# WorkBuddy 接入说明

## 职责

本文件说明如何把 WorkBuddy 作为新版 ReviewRun 的试跑入口，以及试跑前后必须核对什么。正式产品行为以 [`../../docs/PRODUCT_REQUIREMENTS.md`](../../docs/PRODUCT_REQUIREMENTS.md) 为准，STEP 业务框架以 [`../../stock-review.md`](../../stock-review.md) 为准，精确机器编排协议以 [`stock-review/SKILL.md`](stock-review/SKILL.md) 为准。开发和数据库人工作业命令只从 [`../../DEV_WORKFLOW.md`](../../DEV_WORKFLOW.md) 读取。

## 当前状态

- WorkBuddy Skill 已同步新版 ReviewRun 对象和 CLI 参数合同。
- 真实 WorkBuddy 端到端合同验收尚未完成，连续五个交易日试跑也未完成。
- 新版默认使用 `data/review_run.sqlite`；首次获确认的 `daily start` 可以创建 schema version 1。
- 旧 `data/stock_review.sqlite` 保持历史原貌，新版日常运行不得指回旧库，两个数据库没有自动合并能力。
- 旧 CLI 和 `scripts/run_daily_review.ps1` 在验收完成前保留为过渡入口，不能生成新版正式 publication。

因此，WorkBuddy 现在可以用于首轮真实试跑，但不能表述为已经验收的稳定正式入口。

## 用户入口

把本仓库设为 WorkBuddy 工作空间后输入：

```text
复盘 YYYY-MM-DD
```

这句话表示尝试完成一份可供次日使用的正式复盘。它不表示运行诊断，也不自动授权数据库迁移、扩大采集范围或更换模型。

首轮需要补齐：

- 精确交易日期。
- 精确的正式分析 model ID，以及平台支持时的推理强度。
- 新版池为空时，一份小型板块和股票清单。
- 对 RunManifest 与 FinalizationProposal 的两次完整确认。

只有用户明确说“测试连通性”或“测试数据源”时，才执行诊断 profile。诊断结果不能进入 `propose-actions` 或 `finalize`，也不能作为正式复盘。

## 编排责任

WorkBuddy 必须沿唯一对象链编排：

```text
RunManifest
  → ReviewRun
  → EvidenceBundle
  → RevisionBundle
  → FinalizationProposal / ActionSet
  → 唯一当前 publication
```

运行时必须：

1. 只读生成 RunManifest，展示交易日期、来源、有限对象、记录上限、模型、联网、文件与数据库影响，以及完整正文、有效期和 `manifest_hash`。
2. 在同一正文和哈希获确认后启动 ReviewRun，按槽位转交外部证据信封，并在全部槽位终态后封存 EvidenceBundle。
3. 把正式模型请求通过 `prepare-model → complete-model` 留痕；模型响应只修改允许字段。状态、对象、次日检查、STEP 9 背景或 PlanItem 的变化通过完整 `revise` 联合换版。
4. 只读生成 FinalizationProposal，展示最终 RevisionBundle、质量门禁、ActionSet、数据库与文件影响、完整报告预览、有效期和 `proposal_hash`。
5. 在同一提案获确认后执行 `finalize`，原样返回 run、revision、ActionSet、publication、report 的 ID 与哈希，最后运行 `daily verify`。
6. 临时 Base64 和中转文件只放在操作系统临时目录，不写入 `data/evidence/_workbuddy_tmp/` 或 `.workbuddy/memory/`。

WorkBuddy 只编排 CLI、保存用户选择并转交完整对象。它不得直接编辑 SQLite、证据或报告，不得绕过 `daily finalize` 创建正式结果。

## 硬边界

- 用户确认的正式分析 model ID 必须原样进入 RunManifest。actual model ID 缺失、不一致或模型不可用时停止，不静默换模型。
- WorkBuddy 的编排模型不能代替正式分析模型生成未记录的业务结论。
- capture profile 必须按当前日期、授权和有限池生成；不能默认全市场扫描，也不能为补长报告临时扩源。
- `complete-local` 只是保守模板草案。叙述模板、STEP 9、四态 PlanItem 和参与决策没有按正式合同完成时，不得进入提案。
- 核心证据不足、来源失败或对象身份无法核实时保持 `blocked`；不能把采集失败包装成 `non_participation`。
- RunManifest、模型请求或 FinalizationProposal 过期后，必须重新生成并展示完整正文、新哈希和影响，再取得新确认。
- 调用超时或结果不明时停止重复写入，先用相同 ID、CLI 只读命令、数据库投影和审计日志确认现场。

## 分开的确认

以下确认互不替代：

1. RunManifest 的完整正文与 `manifest_hash`。
2. 将已冻结请求交给真实模型的授权。
3. FinalizationProposal、完整报告预览与 `proposal_hash`。
4. Observation 结果 action 与 `action_hash`。
5. 如另行处理旧库，MigrationPlan 与 `plan_hash` 的独立迁移授权。

## 验收完成条件

只有同时取得以下证据，才能把 WorkBuddy 标为已经验收的正式入口，并另行处理旧链删除：

- WorkBuddy 按当前 Base64、哈希、有效期和乐观版本合同完成真实端到端运行。
- 过期 RunManifest、迟到模型响应、过期或冲突提案、错误 action hash 等失败场景都被准确阻断并原样报告。
- 连续五个交易日由新版 `daily finalize` 产生正式 publication，且逐日 `daily verify` 通过。

这些条件不能从文档、Mock 或本地单元测试推定为已经完成。
