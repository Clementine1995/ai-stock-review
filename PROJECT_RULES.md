# PROJECT_RULES.md

## 职责

本文件只保存本项目特有、违反后会破坏正式复盘对象链、证据边界、数据完整性或审计能力，并且可以通过代码审查、文本搜索或自动化测试验证的架构红线。

通用授权与 AI 协作边界仍以 `AGENTS.md` 为准，产品合同以 `docs/PRODUCT_REQUIREMENTS.md` 为准，当前运行状态以 `README.md`、代码、匹配测试和执行前只读核查共同证明。

## 项目定位与正式对象链

1. 本项目只负责以 `stock-review.md` 为主流程的 A 股短线盘后复盘、次日计划、关注池/热点池、Observation 回填和周度学习；不得增加自动交易、实时盯盘、全市场行情平台、纯机器选股或无证据投资建议能力。
2. 新版正式对象链固定为 `RunManifest → ReviewRun → EvidenceBundle → RevisionBundle → FinalizationProposal/ActionSet → 唯一当前 publication`；正式日报、池状态、Observation 和周报必须从这条链产生，不得另建平行正式流程。

## 入口与分层依赖

3. MVP 正式入口仅限 CLI。新版正式链稳定前不得新增 WebUI、后台 worker、实时服务或自动交易入口；旧 `review create-*` 和 `scripts/run_daily_review.ps1` 只能生成 legacy/transition 产物，不能生成新版 publication，真实 WorkBuddy 合同验收通过前不得删除。
4. CLI 层只负责参数解析、错误展示、JSON/Base64 边界和应用服务调用；只读展示可以调用 repository port 查询处理器。CLI 不得直接建立 SQLite 连接或调用第三方 HTTP 接口。
5. 应用服务只编排领域对象、source/client、repository port 和副作用边界。`review_runs` 领域对象、Observation 与 weekly 领域核心只能依赖纯值、领域规则和 port，不得依赖 HTTP client、SQLite adapter 或 Markdown renderer；业务判断不得下沉到 CLI、SQL 或 renderer。
6. `review_framework` 只解析 `stock-review.md` 并生成 STEP 框架，不得依赖数据源、存储实现或 LLM。
7. `evidence` 的 source/client 层是外部数据采集和限定信封规范化的唯一边界；其它业务层不得绕过它直接使用 HTTP 库调用开盘啦、AKShare、Tushare 或其它外部来源。
8. Repository port 只定义持久化能力；SQLite adapter 只保存、读取并原子应用已经验证的 ActionSet，不得在 SQL 拼接或查询中实现市场、计划或交易判断。
9. `reports` 只把上游已经验证的投影确定性渲染为 Markdown，不得采集证据、调用模型、访问 SQLite、改变状态或补造业务判断。

## 正式操作与受限采集

10. 新版每日 CLI 必须依次使用 `daily plan/start/attach-evidence/seal-evidence/complete-local`，按需使用 `prepare-model/complete-model` 或 `revise`，再使用 `propose-actions/finalize`。`daily plan` 和 `daily propose-actions` 只读；所有声明正文、内容哈希、operation ID、有效期和乐观版本期望的写入口必须逐项校验。
11. 真实采集必须由已确认的 RunManifest 绑定交易日期、来源、scope、字段、有限对象、记录上限、输出位置和联网影响；采集实现不得临时扩展清单，默认范围只能服务 STEP、关注池或热点池，禁止默认全市场扫描。
12. 新增数据源必须对应明确 STEP 或反复阻塞判断的硬缺口，并验证字段口径、日期、溯源、授权和稳定性。现有材料已经支持当日事实、五日比较和有限池跟踪后必须停止扩源；不得只为增加报告篇幅接入资金流、龙虎榜、新闻或外围数据。

## 数据、版本与状态完整性

13. RunManifest 必须固定交易日期、允许来源/scope/字段/对象、记录上限、operation ID、联网与写入影响、模型使用上限、有效期和内容哈希；过期、正文变化或哈希不匹配时不得写入。
14. Evidence Capture 必须记录交易日期、来源、样本日期、终态和缺失字段；每个槽位都成为 `available`、`unavailable` 或 `skipped` 后才能封存 EvidenceBundle。EvidenceBundle 封存后不可追加或替换证据。
15. 详细日报的 STEP 标题、顺序和职责必须来自 `stock-review.md`。RevisionBundle 必须让 STEP 1-10、参与决策与 PlanItem 共同换版，不得拼接不同修订的 STEP、对象或计划。
16. 每个 STEP 的模型归纳只能读取本 STEP 获准字段，模型响应只允许修改 `conclusion`、`counter_evidence` 和 `open_questions`。未知字段、跨 STEP 事实、池外对象、阈值、PlanItem，以及没有上游来源的股票代码、名称、交易所、板块归属或市场角色必须拒绝；无来源结论只能标记待确认。
17. 核心证据或分析未完成时结果必须保持 `blocked`。正式 `non_participation` 只能表达证据和核心分析已经完成但没有可执行机会；`actionable` 的每个 PlanItem 必须包含相同来源、指标、单位和观察时点下互斥且穷尽的 `expectation`、`over_expectation`、`under_expectation` 和 `abandon`，并带失效条件和证据引用。
18. 关注池、热点池、交易计划和 Observation 状态变化只能来自用户完整预览并确认的 ActionSet，或明确的 Observation 结果 action；不得隐藏自动写入。Observation 必须包含假设、成立条件、失效条件和证据来源，缺少任一项不得进入批准集合。
19. 每个交易日期只能有一个当前正式 publication。旧 publication、RevisionBundle、ActionSet 和报告 blob 必须保持不可变并记录取代关系；并发或过期期望版本必须整体拒绝，不能形成两个当前版本或部分业务状态。
20. Observation 基础记录和结果修订必须追加且不可变，当前状态由投影得到；`invalid` 不得进入经验候选统计。周报只能冻结当前正式日报、其关联 Observation 当前结果和池事件，后续变化不得静默改写旧周报。

## 文件与模块边界

21. 主源码目录固定为 `src/stock_review/`，内部 Python 导入必须从 `stock_review` 包开始。新增模块不得命名为 `utils.py`、`common.py`、`helpers.py`、`misc.py` 或 `tools.py`；数据源适配文件必须以具体来源或能力命名。正式日报、周报和样例证据的默认目录分别为 `reports/daily/`、`reports/weekly/` 和 `data/evidence/`。

## 双库与数据库迁移

22. 新版正式命令默认只使用 `data/review_run.sqlite`，首次正式启动可以在该独立库初始化 schema version 1。`data/stock_review.sqlite` 保持历史旧链边界，新版日常流程不得默认读写它；两个数据库没有自动合并能力，不得把旧库状态描述成已经迁移或合并。
23. 旧库或其它现有库的结构升级只能先运行只读 `database plan-migration`，完整展示 MigrationPlan 与 `plan_hash`，再取得针对同一正文和哈希的单独明确确认后运行 `database migrate --confirmed`。计划必须在同一只读事务中计算完整 schema、全部规范化表记录及关键 pragma 的数据库逻辑快照指纹，并覆盖已提交 WAL 页；备份必须使用 SQLite Online Backup，以 `integrity_check` 和同一逻辑快照指纹验证。迁移只能登记 legacy 记录和历史文件，不能改写历史产物、补造新版版本链、自动用备份覆盖主库或擅自删除数据库 sidecar。

## 审计与可观测性

24. ActionSet 的业务应用、审计事件和 audit outbox 必须在同一事务提交；其它正式 CLI 写操作必须在领域记录或事件持久化后，以同一 operation ID 幂等追加审计和 outbox。审计至少记录命令、交易日期、对象 ID、输出文件和结果状态；本地日志只是可恢复投影，不能替代数据库审计事实。数据源失败还必须记录失败来源、原因和降级结果。

## 测试边界

25. `stock-review.md` 解析测试必须完整识别当前 STEP 1-10；Evidence 缺口测试必须分别覆盖指数、成交额、情绪、板块和个股；Observation 测试必须拒绝缺少成立条件、失效条件或证据来源的记录。SQLite 结构变化必须运行迁移与 repository 匹配测试并说明是否需要迁移；架构依赖测试放在 `tests/architecture/`，自动化测试使用临时数据库和受控替身，不得把 Mock 结果写成联网、真实模型、迁移或正式业务库证据。

## 红线验证映射

| 规则 | 验证入口 | 通过标准 |
| --- | --- | --- |
| 1-3 | `README.md`、`docs/PRODUCT_REQUIREMENTS.md`、`tests/architecture/test_review_run_boundaries.py`、`tests/test_daily_review_script.py` | 只有一条正式对象链和 CLI 命令树；旧入口不能产生新版 publication。 |
| 4-9 | `tests/architecture/test_review_run_boundaries.py` | CLI、领域、框架、source/client、repository 和 renderer 依赖方向全部通过。 |
| 10-12 | `tests/test_review_run_cli_e2e.py`、`tests/test_review_run_capture_boundary.py`、`tests/test_review_run_collection.py` | 预览不写入；写入口校验身份与版本；采集对象、字段和数量不越过 RunManifest。 |
| 13-15 | `tests/test_review_run_domain.py`、`tests/test_review_run_capture_boundary.py`、`tests/test_complete_daily_review.py` | Manifest 和 Evidence 不可篡改，全部槽位终态后才能封存，STEP 与计划共同换版。 |
| 16 | `tests/test_complete_model_review.py`、`tests/test_review_run_actionable_observation.py` | 模型不能新增未冻结事实、身份、阈值、对象或计划，跨 STEP 内容被拒绝。 |
| 17-20 | `tests/test_review_run_domain.py`、`tests/test_review_run_actionable_observation.py`、`tests/test_finalize_daily_review.py`、`tests/test_review_run_repository.py`、`tests/test_create_weekly_review_run.py` | 质量门禁、四态计划、ActionSet 原子性、唯一 publication、Observation 与周报不可变约束全部通过。 |
| 21 | `tests/architecture/test_review_run_boundaries.py`，以及对 `src/stock_review/` 的模块名和导入路径检查 | 新模块位于规定目录、使用绝对内部导入且没有禁止名称。 |
| 22-23 | `tests/test_review_run_migration.py`、`tests/test_review_run_repository.py` | 双库边界明确；计划只读；哈希、WAL、Online Backup、并发门禁、失败恢复和 legacy 登记测试通过。 |
| 24 | `tests/test_audit_log_projection.py`、`tests/test_review_run_repository.py`、`tests/test_finalize_daily_review.py` | 业务事实与 outbox 原子提交，投影可幂等补齐，失败不产生部分业务状态。 |
| 25 | `tests/test_review_framework.py`、`tests/test_review_run_domain.py`、`tests/test_observation.py`、`tests/architecture/test_review_run_boundaries.py` | STEP、缺口、Observation、SQLite 与架构测试覆盖存在并通过；测试不接触正式资源。 |

## 变更规则

- 修改本文件中的红线属于架构决策，必须说明原规则、目标规则、现有违反量、迁移范围和测试变化。
- 放宽红线必须获得用户明确确认，不得通过删除测试、缩小搜索范围、增加例外名单或改写文档绕过。
- 红线变化后必须同步对应架构测试、产品或需求合同以及验证映射。
