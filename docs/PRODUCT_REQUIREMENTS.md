# ReviewRun 正式产品需求

## 1. 文档地位与当前状态

本文是当前定版产品合同。`stock-review.md` 定义 STEP 1-10 的业务框架和用户纪律；本文定义正式运行、版本、确认、发布、存储和验收规则。项目文档的职责与权威范围见 [`README.md`](README.md)。根目录 `PRD.md` 是上一版失败项目材料，只能作为经验参考，不能作为当前实现或验收依据。

本文描述目标行为，不自动证明代码、真实数据源、模型、数据库迁移或 WorkBuddy 验收已经完成。当前实现事实必须由根目录 README、代码、匹配测试和对应环境的只读核查共同支持。

当前代码已经实现 ReviewRun 领域对象、版本化每日 CLI、模型请求闭环、联合最终确认、Observation 结果修订、版本化周报、数据库迁移计划和审计 outbox 投影。当前启用与验收状态如下；其中明确标为尚未完成的事项不得提前写成完成：

- 新版正式运行默认使用独立的 `data/review_run.sqlite`；文件不存在时由首次 `daily start` 初始化 schema version 1。
- 本地旧 `data/stock_review.sqlite` 尚未迁移。它不再阻塞新版首次使用，新版命令默认不得读写该旧库。
- 尚未完成真实 WorkBuddy 合同验收。
- 尚未完成连续五个交易日的新版正式链路试跑。
- 旧 CLI 与 `scripts/run_daily_review.ps1` 尚未删除；它们只属于阶段 4 前的过渡入口，不能生成新版正式 publication。
- 本文不把历史报告改写成新报告，也不声称真实数据源或模型已经联网验证。

## 2. 产品定位

### 2.1 一句话定位

一个面向有个人交易框架的 A 股短线投资者的本地优先盘后复盘系统：它按 `stock-review.md` 组织够用证据、复盘判断、次日条件计划、Observation 回填和周度学习，并以不可变版本和显式确认保证每个结论可追溯。

### 2.2 目标用户

目标用户是已经有复盘方法、愿意对最终判断负责的个人投资者。用户需要系统整理证据、暴露缺口、约束对象链并保存自己的决定，而不是让系统代替自己选股或下单。

### 2.3 核心价值

- 始终按同一 STEP 1-10 顺序复盘，减少遗漏和叙事漂移。
- 把事实、上游判断、反向证据、未验证问题和次日检查分开保存。
- 让证据、模型请求、人工修订、正式计划和发布结果形成可验证哈希链。
- 在证据不足时稳定输出 `blocked`；只有核心证据充分、分析完成但没有可执行机会时才输出明确不参与，避免把采集失败包装成长报告。
- 把计划结果变成追加式 Observation 修订，周报只汇总可追溯的正式样本。

### 2.4 明确不做

- 自动交易、下单、实时盯盘、仓位执行或收益承诺。
- 全市场行情平台、全量资讯库、纯机器选股或默认扫描全市场。
- 把来源数量、字段覆盖率或报告篇幅当作质量指标。
- 让模型新增事实、阈值、股票身份、市场角色、对象或 PlanItem。
- 让 renderer 采集数据、补判断、改变计划或写业务状态。
- 在正式 CLI 闭环稳定前新增 WebUI、后台 worker 或隐藏自动写入口。
- 未经独立确认迁移现有数据库、联网采集、调用模型或执行真实业务写入。

## 3. 唯一正式生命周期

所有新正式日报必须遵守同一对象链：`RunManifest → ReviewRun → EvidenceBundle → RevisionBundle → FinalizationProposal/ActionSet → 唯一当前 publication`。

```text
RunManifest
  → ReviewRun
  → EvidenceBundle
  → RevisionBundle
  → FinalizationProposal / ActionSet
  → 唯一当前 publication
```

### 3.1 RunManifest

`RunManifest` 是一次运行的授权清单，至少包含：

- `manifest_id`、`run_id`、`trade_date`、创建时间、过期时间和 `content_hash`。
- 有序 capture slots；每个槽位包含来源、scope、operation ID、输入/输出位置、是否联网、允许日期/字段、可选的对象/记录上限和写入影响。
- 模型使用模式、允许的 model IDs 和最大请求次数。
- 本次可能产生的文件影响与数据库影响。

`daily plan` 只读生成清单，不创建目录、数据库记录或日志。清单正文变化、过期或哈希不一致时必须重新计划，完整展示新正文、hash、有效期和影响后重新确认。运行中清单过期时，后续写操作必须拒绝；调用方应停止当前运行，以新获确认清单创建新的 ReviewRun，不能沿用旧确认续跑。

### 3.2 ReviewRun

`daily start` 在清单通过完整正文与 `manifest_hash` 确认后创建 `ReviewRun`。运行由追加式事件投影得到当前状态，状态顺序固定为：

```text
collecting → evidence_ready → draft_ready → published
```

同一 operation 的完全相同重放可以幂等返回；相同 ID 对应不同内容、越过状态或基于过期版本的操作必须拒绝。

### 3.3 EvidenceBundle

直接采集来源由 `daily start` 按 manifest 顺序处理。外部来源只能通过 `daily attach-evidence` 提交与槽位绑定的 Base64 JSON 信封。每个槽位最终必须是：

- `available`：存在通过范围、日期和字段校验的证据；
- `unavailable`：来源失败或没有可用证据，并保存原因；
- `skipped`：条件 capture 的谓词不成立。

只有所有槽位进入终态后，`daily seal-evidence` 才能生成不可变 `EvidenceBundle`。封存后不能补证据；新证据必须进入新的 ReviewRun。

### 3.4 RevisionBundle

`RevisionBundle` 是 STEP 1-10、参与决策和计划项的共同版本。它必须绑定：

- `run_id`、交易日期、EvidenceBundle ID/哈希和证据截点；
- 框架版本/哈希、程序版本、renderer 版本；
- 上一修订 ID、修订类型、参与决策、STEP 1-10；
- 候选、批准和拒绝的 PlanItems；
- 使用过的模型响应哈希和本修订内容哈希。

`daily complete-local` 从 EvidenceBundle 生成保守草案。它不得把缺口变成事实，也不自动创建可执行计划。

草案必须按 `STEP_EVIDENCE_REQUIREMENTS` 最低能力组计算 `evidence_gaps`，同组替代能力满足一种即可；STEP 9 按十二模式分别计算背景与触发缺口。存在任意 capture 引用不能清空缺口，缺口不由 scope 名称或 missing_fields 字符串推断。正式不参与修订不能保留 STEP 1-6 的最低能力缺口；STEP 9 已判定 `not_matched` 的模式仍保留未使用的触发材料缺口。

发布前必须消除所有 STEP 的通用模板。仅需结论、反证和待确认问题修订时，可使用已授权的受约束模型；状态、次日检查、对象、STEP 9 背景判断和 PlanItem 需要完整 `daily revise`。两条修订路径不能同时跳过后直接发布本地模板。模型限制不因草案质量不足而放宽。

STEP 9 的能力材料可用不代表背景匹配；本地草案不自动判断语义或绑定对象。`base_evidence_available_trigger_pending` 和 `assessment_pending` 不能作为正式定稿结果；必须联合修订为有证据支持的逐模式判断，背景匹配需绑定对象。不参与也必须把十二模式收敛为 `confirmed` 或 `not_matched`；后者可保留未使用的触发材料缺口。

推荐 capture profile 由调用方依据授权和真实池生成，通过 `--capture-profile-base64-stdin` 将 `allowed_objects` 和 `max_records` 固定到 RunManifest。标准组合为 AKShare 指数与两市量能、hhxg 情绪/板块快照、5 日 index-history、有限主线 sector-history、有限池 stocks（含身份、板块、角色来源和日线历史）。CLI 不隐式增加联网或采集范围。缺少竞价、分时、次日反馈时保留缺口，不为覆盖字段盲目扩源。

`daily revise` 用完整编辑对象追加一份人工联合修订。STEP、对象和计划项不能各自独立覆盖旧记录；任何修改都生成新的 RevisionBundle。

### 3.5 模型阶段

模型阶段采用两段式协议：

1. `daily prepare-model` 保存不可变 request 和 request fingerprint，并追加 `model_requested` 事件。
2. `daily complete-model` 校验 request ID、request hash、manifest hash、EvidenceBundle、请求中已确认且由响应原样回传的 model ID、基础修订和期望当前修订，再保存响应并追加新 RevisionBundle。供应商返回的 actual model ID 由 WorkBuddy 在提交前取得并比对；当前 ReviewRun schema 不单独保存 `actual_model_id`，actual ID 缺失或不一致时不得调用 `complete-model`。

模型请求使用 schema version 2。`frozen_input.normalized_evidence_by_step` 按 STEP 1-10 保存各 capture 经同一职责白名单裁剪后的标准化正文和证据引用，不再包含可跨 STEP 读取的完整合并快照。模型生成某一步时只能读取同编号视图；请求指纹覆盖这些逐步视图。投影视图只保留来源与样本日期等必要溯源元字段，schema 版本和质量统计不能成为业务事实或数字的证据。

模型响应只允许修改各 STEP 的：

- `conclusion`
- `counter_evidence`
- `open_questions`

模型不得修改或新增证据引用、次日检查、完整性问题、证据缺口、阈值、对象身份、PlanItem 或其它业务字段。响应到达时 current revision 已变化，必须按迟到响应拒绝；不能把它自动变基到新修订。

当 RunManifest 的模型模式为 `required` 时，当前运行必须存在已完成的授权模型响应，且当前定稿修订必须继承该响应哈希；否则 `daily propose-actions` 与 `daily finalize` 均拒绝。

### 3.6 FinalizationProposal 与 ActionSet

`daily propose-actions` 基于当前草案和用户 selections 先执行发布质量校验。存在阻断项时命令拒绝并返回问题，不生成可提交提案；通过后只读生成：

- 一份 `final` RevisionBundle；
- 完整质量门禁结果；
- 一个包含发布、池状态、Observation 创建及可选 Observation 回填动作的 `ActionSet`；
- 每个动作目标的期望旧版本、文件影响和数据库影响；
- 带有效期、内容哈希和完整 Markdown 预览的 `FinalizationProposal`。

该命令不写报告、不应用动作、不创建 publication。用户必须看到完整提案、预览、影响和 `proposal_hash` 后，才能单独确认 `daily finalize`。

`daily finalize` 重新校验提案哈希、有效期、当前修订和每个目标的期望版本；随后保存不可变文件，并幂等保存最终 RevisionBundle、FinalizationProposal 与 ActionSet 作为未应用中间记录。`apply_action_set` 再用一个 SQLite 事务原子应用 publication、池事件、Observation/结果修订、审计 outbox 和应用结果；部分业务动作不能单独成功，未应用中间记录也不能称为正式 publication。

### 3.7 唯一当前 publication

每个交易日期只能有一个 current daily publication。重新正式发布时：

- 新 publication 显式引用它取代的 publication；
- 旧 publication、旧 RevisionBundle 和旧报告 blob 保持不可变；
- “当前版本”以数据库 publication 投影为准，不能靠文件修改时间或文件名猜测；
- 没有 `daily finalize` 成功记录的旧报告不能称为新版正式 publication。

## 4. STEP 1-10 产品目标

标题、顺序和规则正文来自 `stock-review.md`。每个 `StepAssessment` 必须保存状态、结论、证据 refs、反向证据、待确认问题、次日检查、对象/方向/计划映射、完整性问题和证据缺口。STEP 9 还必须保存编号 1-12 的 `pattern_assessments`，每项明确模式名、状态、对象、自己的证据 refs 和缺口。renderer 只能陈列这些字段。

| STEP | 目标 | 关键边界 |
| --- | --- | --- |
| 1 判断大盘所属阶段 | 结合指数、两市量能和必要外围证据，判断市场阶段或保留待确认。 | 单日快照不能伪称连续历史；外围只在与 A 股复盘相关且有来源时使用。 |
| 2 判断目前市场的风格 | 区分趋势风格与情绪连板风格及其相对强弱。 | 风格是基于证据的判断，不从热门叙事或单只个股外推。 |
| 3 情绪周期判断 | 依据涨跌停、炸板、连板高度/梯队、晋级与反馈等证据判断情绪阶段。 | 缺少关键情绪证据时必须保留缺口，不能确认周期标签。 |
| 4 核心板块 | 从已有板块证据和赚钱效应识别方向候选。 | 不聚合同名/近义题材为新对象，不把资金榜、涨停榜等不同口径拼成一个指标。 |
| 5 主升板块及阶段 | 判断核心方向是否进入或处于主升，以及所处阶段。 | 必须能追溯至 STEP 4 方向和连续比较；证据不足时可明确“不可判定”。 |
| 6 核心票 | 在已确认名称、代码、交易所、板块归属和市场角色的对象中识别核心票。 | 股票身份和角色不能由模型猜测；STEP 7 对象只能来自本步集合。 |
| 7 重点关注 | 从 STEP 6 集合收敛次日重点核验对象。 | 只定义关注对象和核验理由，不绕过 PlanItem 直接产生交易动作。 |
| 8 预演 | 为同一组计划对象形成可观察、互斥且穷尽的四态预演。 | 四态使用同一来源、主指标、单位和观察时点；不得出现边界重叠或空档。 |
| 9 几种模式买点，每天找一找 | 对框架列出的十二种模式逐项核验是否有被证据支持的具体对象。 | 模式名本身不是买点；每项只使用自己的 refs，缺少对象或背景/触发证据时只能记录未满足、不可用或触发待核验。 |
| 10 最终输出复盘文档 | 汇总参与决策、质量处置、STEP 1-10、正式计划、Observation 映射和溯源。 | 只有通过质量门禁的 final RevisionBundle 才能进入提案；正式 publication 只能由 finalize 产生。 |

用户纪律和模式清单保持在 `stock-review.md`。它们属于用户框架约束，不是由历史回测验证的收益承诺。

## 5. 正式计划、质量和不参与

### 5.1 PlanItem

每个行动计划项必须具有：

- 稳定的 plan/object ID；已确认的对象名称；股票还需合法六位代码、交易所和板块归属。
- 市场角色、角色来源、角色确认状态和角色证据 refs。
- 与 STEP 4/5 方向的关联；无关联时必须说明独立原因。
- 可验证假设、成立条件、失效条件、行动类型、最大风险、风险单位、证据 refs 和回填期限。
- `expectation`、`over_expectation`、`under_expectation`、`abandon` 四个 scenario bands。

数值条件必须从负无穷到正无穷连续覆盖，相邻边界只能属于一档；枚举条件必须互斥并完整覆盖声明的全集。STEP 7、8、10 的对象与 PlanItems 必须保持一致。

### 5.2 质量门禁

正式提案检查 G1-G6：STEP 与身份完整性、对象链、四态条件、最终参与决策、哈希/版本溯源和 Observation 可回填性。结果分为：

- `blocked`：存在完整性、身份、对象链、条件、版本或回填阻断，不能发布。
- `non_participation`：STEP 1-6 均已完成、有证据引用且没有最低能力缺口；STEP 9 已完成、有证据引用，十二模式均为 `confirmed` 或 `not_matched`；最终没有行动结构。已判定 `not_matched` 的模式可保留未使用的触发材料缺口，相关行动门禁可标记为不适用。
- `actionable`：所有行动结构和回填要求通过，可以进入最终确认。

不参与是一份有证据支持的合法正式结论，不能为了“每天必须有票”降低门禁。来源失败、核心证据不足、分析未完成或无法判断都必须保持 `blocked`。renderer 必须展示门禁结果和阻断项，不能自行把 `blocked` 改为可发布。

`propose-actions` 与 `finalize` 均拒绝未处理通用模板；non_participation 不能保留核心证据缺口。renderer 的局部空集合显示“无反向证据”“未绑定对象”“未生成正式交易计划”等字段语义，不能宣称整个上游输入为空。

## 6. Observation 和周度学习

### 6.1 Observation

每个最终批准的 PlanItem 都在 ActionSet 中映射为一条不可变 Observation。Observation 必须包含假设、成立条件、失效条件、证据引用和最晚回填日期。

结果回填采用单独的两步确认：

1. `observation propose-result` 只读生成带当前结果版本期望、有效期和内容哈希的 action。
2. `observation record-result` 接收完整 action 和 `action_hash`，追加结果修订并更新当前投影。

结果状态为 `hit`、`miss` 或 `invalid`。`miss`/`invalid` 必须使用固定原因分类，所有结果都必须有实际结果和证据引用。基础 Observation 和历史结果不被覆盖；`invalid` 不进入有效经验候选统计。

### 6.2 周报

`weekly create` 只从周期内的 current daily publications 向下冻结：

- 日报 publication ID、report hash 和对应 RevisionBundle；
- 与这些正式修订关联的 Observation 及其当前结果修订；
- 与这些日报 ActionSets 关联的池状态事件。

没有正式日报时拒绝生成。周报输入、revision、报告 blob 和 publication 均使用内容哈希；同一周期只保留一个 current weekly publication，旧版本保持不可变。

## 7. 正式 CLI 合同

统一入口是：

```powershell
.\.venv\Scripts\python.exe -m stock_review.cli <command>
```

JSON 正文使用 UTF-8 Base64 从 stdin 输入。`ID_OR_null` 表示需要显式传 ID 或字面量 `null`。

### 7.1 Daily

```text
daily plan --date DATE ((--capture SOURCE:SCOPE | --conditional-capture SOURCE:SCOPE?PREDICATE)+ | --capture-profile-base64-stdin) [--output-dir DIR] [--evidence-dir DIR] [--database DB] [--model MODEL_ID]... [--model-required]
daily start --manifest-base64-stdin --manifest-hash HASH [--database DB] [--audit-log LOG]
daily attach-evidence --run-id ID --date DATE --manifest-hash HASH --operation-id ID --input-base64-stdin [--database DB] [--audit-log LOG]
daily seal-evidence --run-id ID --date DATE --manifest-hash HASH [--database DB] [--audit-log LOG]
daily complete-local --run-id ID --date DATE --manifest-hash HASH --expected-current-revision-id ID_OR_null --operation-id ID [--framework PATH] [--database DB] [--audit-log LOG]
daily prepare-model --run-id ID --date DATE --base-revision-id ID_OR_null --expected-current-revision-id ID_OR_null --model-id ID --operation-id ID [--database DB] [--audit-log LOG]
daily complete-model --run-id ID --date DATE --request-id ID --base-revision-id ID_OR_null --expected-current-revision-id ID_OR_null --model-response-base64-stdin --manifest-hash HASH --operation-id ID [--framework PATH] [--database DB] [--audit-log LOG]
daily revise --run-id ID --base-revision-id ID --expected-current-revision-id ID --edits-base64-stdin --operation-id ID [--database DB] [--audit-log LOG]
daily propose-actions --run-id ID --review-revision-id ID --selections-base64-stdin [--database DB] [--output-dir DIR]
daily finalize --proposal-base64-stdin --proposal-hash HASH [--database DB] [--audit-log LOG]
daily show --run-id ID [--database DB]
daily verify --run-id ID [--database DB]
```

`--capture`、`--conditional-capture` 和 `--model` 均可重复；`--model-required` 必须和至少一个 `--model` 同时使用。

### 7.2 Observation 与 weekly

```text
pool list [--type watch|hot] [--record-kind real|sample] [--database DB]
observation list [--date DATE] [--status pending|hit|miss|invalid] [--due-on-or-before DATE] [--database DB]
observation propose-result --observation-id ID --expected-result-revision ID_OR_null --status hit|miss|invalid --actual-result TEXT [--reason REASON] --evidence-reference REF [--evidence-reference REF]... [--note TEXT] [--database DB]
observation record-result --action-base64-stdin --action-hash HASH [--database DB] [--audit-log LOG]
weekly create --end-date DATE [--start-date DATE] [--database DB] [--output-dir DIR] [--audit-log LOG]
```

### 7.3 Database

```text
database plan-migration --database DB --backup-path BACKUP [--target-version 1] [--evidence-dir DIR] [--daily-report-dir DIR] [--weekly-report-dir DIR]
database migrate --database DB --plan-base64-stdin --plan-hash HASH --confirmed [--audit-log LOG]
database project-audit-log [--database DB] [--log LOG] [--limit N]
```

旧的 `review create-brief`、`review prepare-detailed`、`review create-detailed`、旧的 pool/Observation 写命令和 PowerShell 日常脚本不属于本节正式合同。它们在真实 WorkBuddy 合同验收前保留以便迁移核对，但输出只能按 legacy/transition 处理。

## 8. 数据库迁移、历史数据和审计

### 8.1 新旧数据库边界

新版正式库固定为 `data/review_run.sqlite`。路径不存在或数据库无表时，只有 `daily start` 可以初始化 schema version 1；其它新版命令仍要求结构完整。旧 `data/stock_review.sqlite` 保持历史原貌，新版默认命令不得读写它。旧池、Observation 和报告不会自动进入新版投影，当前也没有跨库合并能力。

新版首次运行不以旧库迁移为前置条件。旧库迁移只属于单独的历史核对或原地升级任务，不能和日常复盘混为一次操作。

### 8.2 两段式迁移确认

以下协议只处理命令明确指定的目标库，不会把旧库合并进已经使用的 `data/review_run.sqlite`。采用独立新版库后，不得把旧库迁移描述为历史自动接入方案。

`database plan-migration` 必须全程只读，输出的 `MigrationPlan` 绑定：

- 目标本地数据库绝对路径和数据库逻辑快照指纹；`database_sha256` 字段是在同一只读事务内对完整 schema、全部表记录（BLOB 规范化）以及 `application_id`、`user_version` 关键 pragma 计算的 canonical hash，包含已提交但尚未 checkpoint 的 WAL 页，不是主文件逐字节哈希；
- 当前/目标 schema version；
- 备份路径；
- 旧表计数与内容哈希；
- 历史文件路径与字节哈希；
- 预计 legacy 登记数、回查 SQL 和失败恢复步骤；
- 完整计划的 `content_hash`，即待确认 `plan_hash`。

只有用户看过完整计划并针对该 `plan_hash` 单独明确确认，才能执行 `database migrate --confirmed`。此前对代码修改、测试、真实复盘或 WorkBuddy 的授权都不能代替迁移授权。

执行时必须先取得 SQLite `BEGIN IMMEDIATE` 单写者门禁，再用数据库逻辑快照指纹、旧表计数和表哈希确认目标库从计划生成后没有变化，且备份路径不与目标库相同。门禁持有期间通过 SQLite Online Backup 建立一致性快照，并用 `PRAGMA integrity_check` 与计划中的数据库逻辑快照指纹验证；历史产物文件仍按各自字节哈希验证。

旧表重命名、新 schema、legacy 登记和迁移审计必须处于上述同一事务；迁移失败或进程中断时由 SQLite 原子回滚。再次执行同一计划前必须确认目标库仍匹配计划，并验证备份文件存在、完整且逻辑指纹一致；已成功迁移的幂等重放也必须通过备份核验。程序不得用旧备份自动覆盖可能包含并发提交的主库，也不得删除主库 sidecar；必须报告原始迁移错误。不能把主文件字节比较当作快照一致性证据，也不能自行运行 Git 回滚命令。

### 8.3 legacy 规则

旧表记录、`reports/daily/`、`reports/weekly/` 和旧 Evidence 文件只登记到 `legacy_imports`，保存来源、路径或主键、哈希与分类。迁移不得：

- 改写历史 Markdown；
- 把旧报告包装成新版 current publication；
- 猜测缺失的 RunManifest、EvidenceBundle、RevisionBundle 或 ActionSet；
- 用当前代码重新渲染历史内容并覆盖原文件。

### 8.4 审计

ActionSet 的业务应用、审计事件和 audit outbox 在同一事务保存。其它正式写命令在领域记录或事件持久化后追加审计与 outbox，并用同一 operation ID 的幂等重试补齐中断点。`database project-audit-log` 幂等投影待投递 outbox 到本地日志；投影失败不应改变已经提交的业务事实，后续可重放。数据库审计记录是事实来源，日志是可恢复投影。

## 9. 文件与发布布局

```text
data/evidence/<trade-date>/<run-id>/captures/<slot-id>.json
reports/daily/<trade-date>/<run-id>/revisions/<review-revision-id>.md
reports/daily/<trade-date>/<run-id>/blobs/<report-hash>.md
reports/weekly/blobs/<report-hash>.md
logs/review_run.log
```

正式日报必须固定包含顶部决策区、质量处置及阻断项、按框架顺序的 STEP 1-10、正式交易计划、Observation 映射和证据/版本技术附录。报告中的事实、判断、条件和 refs 全部来自已验证 RevisionBundle/QualityAssessment/ActionSet 投影。non_participation 报告使用紧凑正文，重复缺口集中呈现一次；正式模型未使用时明确写“未使用正式模型通道”。次日检查只能包含可观察的市场、板块或个股指标、单位和时点，不能出现 CLI、capture/source、`allowed_objects` 或补采操作说明。

正文必须给出 publication ID、ActionSet ID 及其权威数据库哈希定位。由于 ActionSet 内容哈希包含报告哈希，而 publication 内容哈希在正式提交时生成，这些包裹哈希不能自嵌入同一份被哈希正文；`daily finalize` 回执返回实际 `action_set_hash`、`publication_hash` 和 `report_hash`，`daily verify` 按 ID、数据库记录与报告文件联合核验。提案预览和最终 blob 使用相同字节，正文只标记内容已 `finalized`，是否正式提交及是否为当前版本以 publication 投影为准。

## 10. 架构与依赖方向

- CLI 只负责参数解析、错误展示、JSON/Base64 边界和调用应用服务；只读展示可以调用 repository port 查询处理器。CLI 禁止直接使用 SQLite 连接或第三方 HTTP。
- 应用服务负责把 manifest、领域规则、source/client、repository port 和副作用边界编排成用例。应用服务不能在 renderer 或 SQL 中隐藏业务判断。
- `review_runs` 中的领域对象和其它领域核心只依赖纯 Python 值、领域规则与 port；同目录应用服务可以编排 port，但领域对象禁止依赖 HTTP 客户端、SQLite adapter 或 Markdown renderer。
- `evidence` source/client 负责外部调用和信封规范化。业务服务禁止绕过 source/client 直接用 HTTP 库访问数据源。
- repository port 定义持久化能力；SQLite adapter 实现读写与原子 ActionSet，不在 SQL 拼接中推导市场或交易结论。
- `reports` 只负责确定性投影和 Markdown 渲染，禁止采集、持久化、模型调用或新增业务判断。

## 11. 权限与副作用

- 只读：`daily plan`、`daily propose-actions`、`daily show`、`daily verify`、`pool list`、`observation list`、`observation propose-result`、`database plan-migration`。
- 本地业务写：`daily start`、`daily attach-evidence`、`daily seal-evidence`、`daily complete-local`、`daily prepare-model`、`daily complete-model`、`daily revise`、`daily finalize`、`observation record-result`、`weekly create`、`database migrate`、`database project-audit-log`。
- 联网与模型调用不是 CLI 名称暗含的授权。执行含联网 capture 或把请求交给真实模型前，必须按运行 manifest 和当前会话授权单独界定来源、日期、对象、范围与影响。
- 数据库迁移必须使用专门的 plan/hash/confirmed 协议，不能与一般本地写授权合并。

## 12. 验收标准

### 12.1 已实现代码的合同验收

- 当前 `stock-review.md` 的 STEP 1-10 能按顺序进入 RevisionBundle 和正式 renderer。
- RunManifest、EvidenceBundle、ReviewRunEvent、RevisionBundle、ActionSet、FinalizationProposal 和报告内容均有稳定哈希校验。
- 迟到模型响应、越权字段、未知对象、缺失身份、交叉 STEP 对象、四态重叠/空档、运行中已过期 RunManifest、过期提案和乐观版本冲突被拒绝。
- 缺少核心证据的 non_participation 被拒绝；只有 STEP 1-6 证据充分、STEP 9 十二模式完成核验的不参与结论可以进入提案。
- `daily propose-actions` 不写状态；`daily finalize` 才能联合应用动作并创建 current publication。
- Observation 结果和周报引用不可变版本，重放保持幂等。
- renderer 对相同输入输出相同字节，且不在缺证据时补结论。

### 12.2 阶段 4 完成条件

以下证据全部具备后，才能宣布新版替代过渡链：

1. 真实 WorkBuddy 使用独立新版库，按本文 CLI/Base64/hash/确认合同完成端到端验收，包含失败、迟到响应和重放场景。
2. 使用新版链路连续完成五个交易日的正式 publication，并逐日通过 `daily verify`。
3. 用户确认删除旧 CLI/PowerShell 的范围；删除属于后续独立任务。

在这些条件达成前，正式文档必须继续显示“WorkBuddy 未验收、五日试跑未完成、旧链仍保留”的事实。旧库是否迁移单独陈述，不再作为新版启用状态。
