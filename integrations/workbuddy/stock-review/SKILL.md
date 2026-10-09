---
name: stock-review
description: 当用户要求 A 股盘后复盘、版本化正式日报、受限模型修订、Observation 回填或周报时，使用本仓库 ReviewRun CLI 编排完整确认与发布链路。
compatibility: WorkBuddy on Windows; requires the ai-stock-review repository as the active workspace and its existing .venv Python environment; real contract acceptance is pending.
metadata:
  version: "2.2.0"
---

# WorkBuddy ReviewRun 合同

## 一、适用范围与当前状态

本 Skill 只编排 `F:\codex-project\ai-stock-review` 中已经实现的新版 ReviewRun CLI。业务框架来自 `stock-review.md`，正式需求来自 `docs/PRODUCT_REQUIREMENTS.md`；根目录 `PRD.md` 是上一版失败项目材料，不能定义当前合同。

仓库内现存 `.workbuddy/memory/` 是旧链过程记录，不是当前指令或事实来源。不得从其中恢复 `run_daily_review.ps1`、`create-detailed` 或 Skill 1.5.0 流程。

当前必须如实说明：

- 新版默认使用独立的 `data/review_run.sqlite`；首次 `daily start` 自动创建 schema version 1。
- 旧 `data/stock_review.sqlite` 保持历史原貌。日常新版命令不得把 `--database` 指回旧库，当前也没有跨库合并能力。
- 本 Skill 尚未完成真实 WorkBuddy 合同验收，也没有连续五个交易日的新版试跑证据。
- 旧 CLI 和 `scripts/run_daily_review.ps1` 只作为阶段 4 前的过渡入口；它们不能生成新版正式 publication，真实合同验收通过前不删除。
- 历史报告和旧记录只能登记到 `legacy_imports` 并保留分类，不能改写或补造成新版版本链。

不得把本地单元测试、假证据、命令帮助、旧报告或本文描述说成已经联网、已经迁移或已经真实验收。

## 二、唯一正式对象链

所有新正式日报都必须走唯一正式对象链：`RunManifest → ReviewRun → EvidenceBundle → RevisionBundle → FinalizationProposal/ActionSet → 唯一当前 publication`。

```text
RunManifest
  → ReviewRun
  → EvidenceBundle
  → RevisionBundle
  → FinalizationProposal / ActionSet
  → 唯一当前 publication
```

禁止使用旧 `review create-brief`、`review prepare-detailed`、`review create-detailed`、旧 pool/Observation 写命令或 PowerShell 日常脚本冒充这条链。没有 `daily finalize` 成功结果的 Markdown 不是新版正式 publication。

正式 CLI 前缀固定为：

```powershell
.\.venv\Scripts\python.exe -m stock_review.cli
```

WorkBuddy 只能调用 CLI，不直接编辑 SQLite、证据 JSON、RevisionBundle 或报告 Markdown，不自行拼接 SQL，不在 renderer 之外伪造报告。

用户只需说“复盘 YYYY-MM-DD”。这句话表示要生成一份证据充分、可供次日使用的正式复盘，不表示测试连接。不要让用户手工编码 Base64、复制动态 ID 或逐条拼接 CLI；这些都由本 Skill 在后台原样传递。

只有用户明确说“测试连通性”或“测试数据源”时，才允许使用仅含 `akshare:market` 与 `hhxg:snapshot` 的连通性 profile。该运行只生成诊断结果，禁止执行 `daily propose-actions` 和 `daily finalize`，也不能称为正式复盘。普通“复盘”不得自动降级为这个 profile。

普通复盘在 plan 前必须取得精确的正式分析 model ID；平台支持推理强度时也要取得精确强度。用户尚未指定时只问一次。正式 RunManifest 使用该 model ID 和 required 模式；`model_use=none` 只在用户明确选择不使用正式模型，并亲自提供、核对完整业务结论时使用，且不能绕过核心证据门禁。

## 三、通用执行规则

### 3.1 先展示后提交

以下对象必须展示完整正文、内容哈希、有效期和影响范围后再提交：

1. RunManifest 与 `manifest_hash`。
2. FinalizationProposal、ActionSet、报告预览与 `proposal_hash`。
3. Observation 结果 action 与 `action_hash`。
4. MigrationPlan 与 `plan_hash`。

确认只适用于所展示的同一正文和哈希。正文、目标库、当前版本或文件状态变化后，旧确认失效，必须重新生成并展示。

RunManifest 或 FinalizationProposal 过期后，必须重新生成，完整展示新的正文、哈希、有效期和影响范围，并取得一次新的明确确认。即使业务含义看起来相同，也禁止沿用旧确认。运行中 RunManifest 过期并被后端拒绝时，停止当前运行；重新 plan 并确认后创建新的 ReviewRun，不得自动续跑。提案过期时重新执行 `daily propose-actions`，展示完整新提案与报告预览并重新确认，不能只报新 hash。

### 3.2 Base64 stdin

所有带 `*-base64-stdin` 的参数都接收完整 UTF-8 JSON 的 Base64。不得截断 JSON、重新排序后沿用旧哈希、从多个输出拼接对象或用仓库内临时文件绕过确认。

必须借助临时文件时，只能写入操作系统临时目录，并在本次运行结束后清理。不得创建 `data/evidence/_workbuddy_tmp/`，不得向 `.workbuddy/memory/` 写入本次过程记录。

PowerShell 传输模板：

```powershell
$payloadJson = @'
{"完整对象":"原样放在这里"}
'@
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payloadJson)) |
  .\.venv\Scripts\python.exe -m stock_review.cli <command> --对应-base64-stdin
```

需要表达空基础修订或空当前结果修订时，参数值必须是字面量 `null`，不能省略必填参数或传空字符串。

### 3.3 权限边界

命令名称不自动授予联网、模型或业务写权限。准备执行前必须说明环境为本地、目标交易日、数据源/模型、对象范围、文件影响、数据库影响和回查方式。

- `daily plan`、`daily propose-actions`、`daily show`、`daily verify`、`pool list`、`observation list`、`observation propose-result`、`database plan-migration` 是只读操作。
- 其余新版命令会写本地文件、SQLite 或审计 outbox；只在当前会话授权覆盖相同范围时执行。
- 数据库迁移始终要求单独的 MigrationPlan/hash 确认，不能沿用一般本地写授权。
- 真实连接器或真实模型调用只在当前 manifest 明确包含该来源/模型且用户授权覆盖本次调用时执行。
- 不得自行更换用户已经指定的模型或推理强度。plan 前展示精确 model ID；实际模型不可用时停止并说明，不能静默改用其它模型。
- WorkBuddy 编排模型与正式分析模型是两个不同对象。编排模型只负责执行合同，不能把自己生成的市场结论冒充正式模型结果。平台能返回编排模型的 requested/actual model ID 时，收口一并报告。正式模型调用必须取得平台返回的 actual model ID；actual ID 未返回或与已确认 ID 不一致时停止，不提交响应。正式 request、response 哈希和已确认 model ID 进入 ReviewRun 记录。

## 四、新版数据库边界

日常命令统一使用默认 `data/review_run.sqlite` 和 `logs/review_run.log`。该数据库不存在时，已确认 RunManifest 后的 `daily start` 会初始化完整 schema；不要先运行迁移，也不要为了读取旧池把新版命令指向 `data/stock_review.sqlite`。

旧池、Observation 和历史报告不会自动进入新版投影。首日根据用户当次确认的有限对象生成 capture profile，之后只从新版 ActionSet 累积状态。

只有用户明确提出核对或原地升级旧库时，才进入以下旧库迁移流程。迁移不会把旧库合并到已经使用的新版库。

### 4.1 只读生成旧库迁移计划

```text
database plan-migration --database DB --backup-path BACKUP [--target-version 1] [--evidence-dir DIR] [--daily-report-dir DIR] [--weekly-report-dir DIR]
```

展示完整 MigrationPlan，至少明确目标库绝对路径与数据库逻辑快照指纹、当前/目标 schema、备份路径、旧表计数/哈希、历史文件清单/文件哈希、预计 legacy 登记数、回查 SQL、失败恢复步骤和 `content_hash`。`database_sha256` 字段是在同一只读事务内对完整 schema、全部表记录（BLOB 规范化）以及 `application_id`、`user_version` 关键 pragma 计算的 canonical hash，包含已提交但尚未 checkpoint 的 WAL 页，不是主文件逐字节哈希。

单独询问用户是否确认按该完整计划和 `plan_hash` 迁移。没有明确回答时停止，不得运行 migrate。

### 4.2 提交已确认的旧库计划

```text
database migrate --database DB --plan-base64-stdin --plan-hash HASH --confirmed [--audit-log LOG]
```

只传回刚展示并确认的完整计划。CLI 会先取得 SQLite `BEGIN IMMEDIATE` 单写者门禁，再用数据库逻辑快照指纹、旧表计数/哈希和历史文件哈希复核输入；门禁持有期间通过 SQLite Online Backup 建立一致性快照，并以 `PRAGMA integrity_check` 和同一数据库逻辑快照指纹验证，再在同一事务中创建 schema version 1、登记 `legacy_imports` 和迁移审计。

迁移失败或进程中断时由 SQLite 原子回滚。再次执行同一计划会重新核验目标库，并要求备份存在且逻辑快照匹配；不得用旧备份自动覆盖主库或删除主库 sidecar。原样报告迁移或回查错误；不得以主文件字节是否相同判断一致性，不得手工放宽哈希或自行运行 Git 回滚。

迁移成功返回和计划内回查完成前，不得说旧库已迁移；迁移结果也不得说成已经并入新版库。

## 五、每日正式流程

### 5.1 只读计划 RunManifest

```text
daily plan --date DATE ((--capture SOURCE:SCOPE | --conditional-capture SOURCE:SCOPE?PREDICATE)+ | --capture-profile-base64-stdin) [--output-dir DIR] [--evidence-dir DIR] [--database DB] [--model MODEL_ID]... [--model-required]
```

必须显式填写交易日和至少一个 capture。普通 capture 使用 `source:scope`；条件 capture 使用 `source:scope?predicate`。需要限制对象的组合改用 `--capture-profile-base64-stdin`，并与两个简单 capture 参数互斥。`--capture`、`--conditional-capture` 和 `--model` 都可重复；普通正式复盘必须传精确 `--model MODEL_ID --model-required`。required 模式下，已完成的授权模型响应及其在当前修订中的继承关系是提案和发布硬条件。当前直接来源路由是受支持 scope 下的 `akshare`、`hhxg`；`manual`、`tongdaxin`、`workbuddy` 是外部 Base64 信封来源。只声明来源不证明连接可用。

展示完整 manifest，并用易读清单说明：

- run/manifest ID、交易日、有效期、`content_hash`；
- 每个槽位的 source、scope、operation ID、是否联网、允许日期/字段和输出路径；若清单设置了允许对象或最大记录数，也必须一并展示；
- model mode、model IDs 和最大请求数；
- 全部文件及数据库影响。

用户确认该正文与 hash 后才启动；过期时按 3.1 完整重新展示并确认，不能静默重建后启动。

#### 推荐 capture profile

调用方必须在 plan 前按授权与真实关注池/热点池生成以下够用组合，并展示每个槽位及已知缺口。CLI 不内置隐式默认槽位，因为来源授权、有限对象与输出位置由本次 manifest 决定，不能静默扩大联网或采集范围。新库中的池为空、且当前授权材料无法确定有限对象时，先让用户给出一份小型板块/股票清单，再生成 manifest；不得为了避开这次输入而改用注定缺少核心证据的连通性 profile。

| 推荐槽位 | 内容与用途 | 缺失处理 |
| --- | --- | --- |
| `akshare:market` | 当日指数与沪深 A 股成交额，服务 STEP 1 | 任一交易所量能缺失时保留两市量能缺口。 |
| `hhxg:snapshot` | 当日情绪核心字段和板块榜单，服务 STEP 3–4 | 该来源不提供项目要求的指数、两市量能或已确认股票身份。 |
| `tongdaxin:index-history` | `market.index_history` 中最近 5 个交易日的指数比较，服务 STEP 1 | 无授权或无法取得时明确披露，不自行换源。 |
| `workbuddy:sector-history` | `sector_history` 中有限主线/热点的连续比较，服务 STEP 5 | 必须绑定已确认方向；缺失则保留板块连续比较缺口。 |
| `workbuddy:stocks` | `stocks` 中真实池与有限候选的代码、名称、交易所、板块、角色及来源、日线历史，服务 STEP 2、6–10 | 身份或角色必须来自已确认材料，不能由模型补造。 |

结构化 profile 的 stdin 解码后必须是以下形状；调用方不能传槽位 ID、operation ID、字段白名单、路径或副作用：

```json
{
  "captures": [
    {"source": "akshare", "scope": "market"},
    {"source": "hhxg", "scope": "snapshot"},
    {"source": "tongdaxin", "scope": "index-history"},
    {"source": "workbuddy", "scope": "sector-history", "allowed_objects": ["已确认板块 ID"], "max_records": 1},
    {"source": "workbuddy", "scope": "stocks", "allowed_objects": ["真实池股票代码"], "max_records": 1}
  ]
}
```

`workbuddy:stocks` 与 `workbuddy:sector-history` 缺少非空 `allowed_objects` 时 plan 必须拒绝；省略 `max_records` 时按对象数生成上限。已有同口径授权信封可覆盖相同能力，不重复采集。外部槽位按 attach-evidence 合同提交；`tongdaxin:index-history` 的 normalized 必须包装为 `{"market":{"index_history":[...]}}`，现有顶层 `records` 原始结果不能直接提交。`workbuddy:sector-history` 的 normalized 使用 `sector_history` 数组，记录以 `sector_id`、`code` 或 `name` 命中清单对象。不得把 `stock-identity` 只允许的代码/名称/交易所字段误当成角色证据。板块和个股记录必须限定在当前池或本次确认的有限候选内，不默认全市场扫描。5 日比较材料是推荐业务范围，最低能力检测不等于五日业务验收。

竞价、分时、次日反馈和监管材料只在具体模式与授权需要时加入清单；否则保留模式缺口，不能为了让十二模式全部齐备扩源。封存前核对实际字段，封存后新材料必须另起获确认的 RunManifest。

### 5.2 启动 ReviewRun

```text
daily start --manifest-base64-stdin --manifest-hash HASH [--database DB] [--audit-log LOG]
```

传入已确认的完整 RunManifest。直接来源按 manifest 顺序执行；直接来源失败由服务记录为 `unavailable`。外部信封槽位保持 `pending`，等待下一步。保存返回的 ReviewRun 状态、run ID 和 manifest hash。

### 5.3 提交外部证据

每个外部槽位只能使用 manifest 中该槽位的 `operation_id`：

```text
daily attach-evidence --run-id ID --date DATE --manifest-hash HASH --operation-id ID --input-base64-stdin [--database DB] [--audit-log LOG]
```

信封必须原样声明同一 run、交易日、source、scope、operation ID 和终态；`available` 还必须提供与交易日一致的 sample date、规范化证据及其原始输入，所有状态都应如实列出缺失字段或失败原因。不得提交 manifest 范围外日期、字段或对象，不得把连接器失败伪装为 `available`。

对外部连接器的调用限于 manifest 已授权范围。不得因零命中、歧义或缺字段自动扩大为全市场搜索。

### 5.4 封存 EvidenceBundle

```text
daily seal-evidence --run-id ID --date DATE --manifest-hash HASH [--database DB] [--audit-log LOG]
```

只有全部槽位处于 `available`、`unavailable` 或 `skipped` 终态才能封存。保存返回的 EvidenceBundle ID/hash 和证据截点。封存后不再 attach；新证据必须新建 ReviewRun。

槽位进入终态只表示采集流程结束，不表示证据够用。核心来源失败时可以继续 seal 和 complete-local 生成诊断，但只要 STEP 1-6 的最低能力或完成状态仍有核心缺口，就必须在提案前停止，并建议以新的完整 RunManifest 补采。不得把“来源失败、无法判断”包装成 `non_participation` 发布。

### 5.5 生成本地草案

```text
daily complete-local --run-id ID --date DATE --manifest-hash HASH --expected-current-revision-id ID_OR_null --operation-id ID [--framework stock-review.md] [--database DB] [--audit-log LOG]
```

首次本地草案通常使用 `--expected-current-revision-id null`。该服务只从封存证据生成保守 RevisionBundle，不自动产生已批准计划。保存 revision ID/hash、STEP 缺口、完整性问题和当前 ReviewRun 状态。

`complete-local` 按 `STEP_EVIDENCE_REQUIREMENTS` 最低能力组生成真实 `evidence_gaps`，STEP 9 按模式列缺口。有引用不等于无缺口。发布前必须处理全部通用模板；non_participation 的 STEP 1-6 也必须完成、有证据引用且无最低能力缺口，STEP 9 十二模式必须收敛为 `confirmed` 或 `not_matched`。已判定 `not_matched` 的模式可保留未使用的触发材料缺口。不得手工清空计算得到的缺口；propose-actions 与 finalize 均重新校验。

### 5.6 受约束模型修订

只有 manifest 的 model use 明确允许目标 model ID 时执行。

普通正式复盘必须走此路径处理 conclusion、counter_evidence、open_questions，所有遗留模板都必须处理。只有用户明确选择 `model_use=none` 时，才由用户亲自提供并核对这些完整业务结论；不能把两条修订路径同时跳过。模型成功返回不代表自动具备可发布质量。

先冻结请求：

```text
daily prepare-model --run-id ID --date DATE --base-revision-id ID_OR_null --expected-current-revision-id ID_OR_null --model-id ID --operation-id ID [--database DB] [--audit-log LOG]
```

有本地草案时，base 和 expected current 都传同一个当前 revision ID；没有基础修订时两者都传 `null`。保存完整 request、request ID 和 `request_fingerprint`。

把 request 的 `frozen_input` 作为唯一事实输入，把 `required_output` 作为硬输出合同。`frozen_input.normalized_evidence_by_step` 按 STEP 1-10 分开保存经过职责裁剪的 capture 视图，不包含完整合并快照；生成某一步时只能读取同编号数组中的 `normalized`，并保留 `evidence_reference` 供回查。真实模型响应必须只有顶层 `steps`，每项只有：

```json
{
  "step_number": 1,
  "conclusion": "...",
  "counter_evidence": ["..."],
  "open_questions": ["..."]
}
```

模型不得新增事实、数字、阈值、证据引用、次日检查、完整性问题、证据缺口、对象、身份、市场角色或 PlanItem。WorkBuddy 自己生成的结论、反证或待确认问题也属于模型文字，必须通过本节的 `prepare-model → complete-model` 留下 request/response 记录；禁止经 `daily revise` 写入后标成“用户确认”。模型调用完成后提交：

```text
daily complete-model --run-id ID --date DATE --request-id ID --base-revision-id ID_OR_null --expected-current-revision-id ID_OR_null --model-response-base64-stdin --manifest-hash HASH --operation-id ID [--framework stock-review.md] [--database DB] [--audit-log LOG]
```

响应到达前必须以 `daily show` 或已保存当前投影确认 revision head 未变化。CLI 拒绝迟到响应、错误 request fingerprint、越权字段，以及本 STEP 冻结证据之外的股票代码或数值；失败后不得改 response 来绕过边界或自动变基。

### 5.7 完整人工联合修订

```text
daily revise --run-id ID --base-revision-id ID --expected-current-revision-id ID --edits-base64-stdin --operation-id ID [--database DB] [--audit-log LOG]
```

base 和 expected current 必须是同一当前草案。`edits` 只允许 `steps`、`candidate_plan_items`、`decision_note`；必须同时提交全量 STEP 1-10 和全量 candidate PlanItems。不能局部覆盖某一步后保留不一致计划。

人工修订也必须通过证据白名单、身份、对象链和四态结构校验。它只承载已验证证据推导出的结构状态、对象/计划联动和用户逐字选择；用户原话与 WorkBuddy 建议要分开，不得把 WorkBuddy 摘要、改写或模型文字冒充“用户确认”。

涉及 status、evidence_gaps、next_day_checks、对象、STEP 9 背景或 PlanItem 时必须执行 daily revise。提交前逐步核对具体事实；核心证据不足时停止，不能用文字说明替代。涉及行动时一起更新 STEP 6→7→8→10 和计划；已有模型修订也不能替代结构修订。

STEP 9 的 `base_evidence_available_trigger_pending` 仅表示能力材料可用，背景与对象仍未核验；`assessment_pending` 表示材料齐备但判断未完成。不得仅凭字段存在声称背景匹配。actionable 发布前必须完成经验证的人工联合修订，逐模式选择诚实状态；背景匹配须绑定已验证对象，不能把上述两个待判断状态当作 actionable 结果。`non_participation` 只表示核心证据充分、分析已经完成，但没有符合框架的可执行机会；它不能作为缺数据、来源失败或无法判断时的兜底。

`next_day_checks` 只能写次日可直接观察的市场、板块或个股指标、单位与时点。不得写 CLI 命令、capture/source 名、`allowed_objects`、补采步骤或其它内部操作说明；技术待办统一留在证据缺口或附录。

### 5.8 只读生成 FinalizationProposal

生成 selections 前先执行 `daily show`。把 `due_observations` 中截至本交易日仍待回填的记录单独展示给用户；只有用户给出实际结果、原因和证据并确认纳入时，才写入 `observation_result_actions`。

```text
daily propose-actions --run-id ID --review-revision-id ID --selections-base64-stdin [--database DB] [--output-dir DIR]
```

`selections` 至少包含：

```json
{
  "participation_decision": "actionable 或 non_participation",
  "decision_note": "用户确认说明",
  "approved_plan_item_ids": [],
  "rejected_plan_items": {"未批准的计划项 ID": "拒绝原因"},
  "pool_actions": [],
  "observation_result_actions": []
}
```

`actionable` 至少批准一个当前候选；`non_participation` 不能批准计划项。每个未批准候选必须且只能给一个拒绝原因。不得批准草案外 ID。

完整展示命令返回的：

- final RevisionBundle、参与决策和所有批准/拒绝项；
- G1-G6 质量结果、处置和全部阻断项；
- ActionSet 中每个发布、池、Observation 创建或结果回填动作；
- 每个目标的期望旧版本、文件影响和数据库影响；
- 正式 Markdown `report_preview`；
- proposal/action/revision/report 的 ID、哈希和有效期。

只读提案不会写报告、应用动作或创建 publication。核心证据缺失时不得调用该命令。质量校验出现 `blocked` 问题时命令拒绝，不会返回可提交的 FinalizationProposal；原样报告全部问题并回到修订阶段。命令成功时仍须把完整提案和完整 `report_preview` 展示给用户，不能只给摘要或结论。

### 5.9 提交唯一正式 publication

只有用户明确确认上一步完整提案和 `proposal_hash` 后执行：

```text
daily finalize --proposal-base64-stdin --proposal-hash HASH [--database DB] [--audit-log LOG]
```

传回同一完整 FinalizationProposal，不得从预览重建对象。CLI 会复核哈希、有效期、current revision 和每个动作目标的期望版本，幂等保存不可变最终 RevisionBundle、提案和 ActionSet，再用一个事务原子应用 publication、池事件、Observation/结果修订、审计 outbox 与应用结果。未应用的中间记录不是正式 publication。提案过期后必须按 3.1 重新生成、完整展示并重新确认；旧确认不能授权新的 proposal/hash。

每个交易日只允许一个 current publication。再次发布保留旧 publication 和报告 blob，并记录取代关系。正式文件位于：

```text
reports/daily/<trade-date>/<run-id>/revisions/<review-revision-id>.md
reports/daily/<trade-date>/<run-id>/blobs/<report-hash>.md
```

数据库 publication 投影决定当前版本，不能按修改时间或熟悉的旧文件名判断。

### 5.10 查看和校验

```text
daily show --run-id ID [--database DB]
daily verify --run-id ID [--database DB]
```

`daily show` 用于展示投影和依赖；`daily verify` 校验整条依赖链，失败返回非零。收口前运行 verify，并原样报告未通过项。verify 通过只说明结构与哈希链有效，不说明市场判断正确。

## 六、Observation 结果

新版 Observation 由已批准 PlanItem 在 finalize 的 ActionSet 中创建，不使用旧 `observation add` 创建新版正式记录。

当前池状态也只读自不可变事件投影；新版池写入只能出现在最终确认的 ActionSet：

```text
pool list [--type watch|hot] [--record-kind real|sample] [--database DB]
```

先只读查看：

```text
observation list [--date DATE] [--status pending|hit|miss|invalid] [--due-on-or-before DATE] [--database DB]
```

先生成结果 action：

```text
observation propose-result --observation-id ID --expected-result-revision ID_OR_null --status hit|miss|invalid --actual-result TEXT [--reason REASON] --evidence-reference REF [--evidence-reference REF]... [--note TEXT] [--database DB]
```

所有结果都必须有实际结果和至少一个证据 ref。`hit` 不填 reason；`miss` 或 `invalid` 的 reason 必须是：

- `judgment_error`
- `insufficient_evidence`
- `observation_or_execution_deviation`
- `invalid_sample`

展示完整 action、当前期望结果版本、影响、有效期和 `content_hash`。用户确认同一 action/hash 后提交：

```text
observation record-result --action-base64-stdin --action-hash HASH [--database DB] [--audit-log LOG]
```

结果以不可变修订追加，基础 Observation 与历史结果不覆盖。版本冲突时重新 list 和 propose，不手工替换 expected version。

## 七、版本化周报

```text
weekly create --end-date DATE [--start-date DATE] [--database DB] [--output-dir DIR] [--audit-log LOG]
```

不传 start date 时取 end date 所在周周一。周报只冻结该周期内 current daily publications、其关联 Observation 当前结果和 ActionSet 池事件；没有正式日报时停止。输出是内容寻址的不可变周报 revision/blob，每周期只有一个 current weekly publication。

不得把 legacy 报告、未 finalize 草案或同日被取代 publication 混入周报。

## 八、审计日志投影

ActionSet 的业务应用、审计事实和 audit outbox 在同一事务保存。其它正式写命令在领域记录或事件持久化后追加审计与 outbox，并用同一 operation ID 幂等补齐。投影待投递日志使用：

```text
database project-audit-log [--database DB] [--log LOG] [--limit N]
```

该命令幂等恢复本地日志。日志只是数据库审计事实的投影，不能用日志缺失推断业务提交失败，也不能用手工日志冒充审计事件。

## 九、报告核对

正式日报固定核对：

- 顶部决策区；
- 质量处置与全部阻断项；
- RunManifest、EvidenceBundle、RevisionBundle、框架、程序、renderer、模型响应的 ID/哈希，以及 ActionSet、publication 的 ID 和权威数据库哈希定位；`daily finalize` 回执中的 `action_set_hash`、`publication_hash`、`report_hash` 必须与数据库记录和报告文件一致；
- 按 `stock-review.md` 顺序且各出现一次的 STEP 1-10；
- 每步的结论、证据 refs、反向证据、待确认问题、次日检查、完整性问题和证据缺口；
- 每个正式 PlanItem 的已确认身份、角色、假设、成立/失效条件、风险、回填日和互斥穷尽四态动作；
- Observation 映射和附录。

renderer 只展示上游数据。报告出现上游没有的事实、阈值、对象或条件时，视为合同失败，不手工编辑报告修复；回到证据或 RevisionBundle 阶段生成新版本。

`non_participation` 报告必须保持 STEP 1-10 标题和顺序，但正文采用紧凑结构：核心判断与证据就近呈现，重复缺口只集中列一次，内部 ID、哈希和运行轨迹放在末尾技术附录。正式模型未使用时明确写“未使用正式模型通道”，不能写“模型待确认”。

报告正文不能自嵌包裹它的 ActionSet、publication 或报告最终哈希，否则会形成循环依赖。提案预览与最终 blob 使用相同字节，正文状态只能说明内容已 `finalized`，是否已提交及是否为当前正式版本以 publication 投影为准；不得写“待最终确认”或在预览阶段写成已发布。

## 十、失败处理与收口

任一命令非零退出时停止依赖该结果的后续步骤，原样报告命令、run/operation/request/proposal/action ID、当前状态和错误。不要自动换来源、扩大对象、放宽校验、修改 hash、删除文件、覆盖旧 revision 或执行数据库迁移。

任务收口只汇报：

- 使用的交易日、run/manifest/evidence/revision/action/publication ID 与哈希；
- 哪些 capture 可用、不可用或跳过，哪些证据仍待确认；
- 是否使用模型以及 request/response 是否通过限制；
- 最终参与决策、质量等级、批准/拒绝计划项和 Observation ID；
- 正式日报路径、`daily verify` 结果、审计投影状态；
- 明确未执行的联网、迁移、五日试跑或旧链删除事项。

不得把技术执行成功表述为投资判断正确或收益保证。
