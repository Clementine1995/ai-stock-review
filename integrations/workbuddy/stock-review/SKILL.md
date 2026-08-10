---
name: stock-review
description: 在 Windows 本地仓库中执行 A 股盘后复盘决策卡流程。用户要求盘后复盘、生成每日决策卡、查看证据缺口、记录人工 STEP 判断或显式创建 Observation 时使用。始终先 DryRun，再取得用户确认；只调用 stock_review 正式 CLI，不直接读写 SQLite，不生成自动交易或无证据投资结论。
compatibility: WorkBuddy on Windows; requires the ai-stock-review repository as the active workspace and its existing .venv Python environment for real runs.
metadata:
  version: "1.0.0"
---

# 盘后复盘助手

## 职责

把 WorkBuddy 作为交互和提醒入口，把仓库中的 `stock_review` CLI 作为唯一业务执行入口。

允许完成：

- 检查实际交易日期对应的每日复盘命令。
- 经用户确认后执行证据采集并生成本地盘后复盘决策卡。
- 读取决策卡，区分事实、证据缺口、有效真实池和未确认系统候选。
- 收集用户原话，经再次确认后调用正式 CLI 保存人工 STEP 判断、STEP 8 预演或 Observation。

禁止完成：

- 不直接访问、编辑或执行 SQL 操作 `data/stock_review.sqlite`。
- 不执行数据库迁移、依赖安装、环境变量修改、文件删除、Git 提交或推送。
- 不使用 `-WithLlm`，不默认生成 STEP 1-10 LLM 详细复盘。
- 不把未确认系统候选放入行动计划，不编造股票代码、名称、板块或行情事实。
- 不生成自动交易指令，不操作券商、交易终端或资金账户。

## 一、确认工作空间和日期

当前 WorkBuddy 工作空间必须是 `ai-stock-review` 仓库根目录。只在当前工作空间检查以下路径，不搜索整台电脑：

- `README.md`
- `stock-review.md`
- `scripts/run_daily_review.ps1`
- `.venv/Scripts/python.exe`

缺少前三项时停止，并提示用户把 WorkBuddy 工作空间切换到仓库根目录。仅真实运行需要 `.venv/Scripts/python.exe`；缺失时停止，不安装依赖。

用户必须提供 `YYYY-MM-DD` 格式的实际交易日期。没有提供时先询问，不得把系统当前日期、自然日或最近快照日期自动当作交易日期。

## 二、先执行 DryRun

确认日期格式后，先运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_daily_review.ps1" -TradeDate "YYYY-MM-DD" -DryRun
```

DryRun 仅展示命令，不联网、不写文件、不调用 LLM。DryRun 失败时停止，不尝试拆开步骤或绕过校验。

DryRun 成功后，向用户说明真实运行的影响范围：

- 联网访问脚本内既有的 AKShare 和 hhxg 数据源。
- 写入或更新该交易日期的 Evidence Snapshot、有限候选历史、SQLite 证据记录、决策卡和本地日志。
- 默认不调用 LLM，不生成详细复盘，不执行交易。
- 任一步失败时脚本停止，后续步骤不会继续。

随后明确询问：`是否确认对 YYYY-MM-DD 执行真实盘后复盘？`

只有用户在当前任务中明确同意后，才能进入真实运行。

## 三、执行真实复盘

用户确认后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_daily_review.ps1" -TradeDate "YYYY-MM-DD"
```

禁止追加 `-WithLlm`。命令返回非零退出码时立即停止，报告失败步骤和原始错误；不自动重试、不改脚本、不单独补跑后续步骤。

成功后只检查以下当日文件：

- `data/evidence/YYYY-MM-DD_snapshot.json`
- `data/evidence/YYYY-MM-DD_review_candidate_history.json`
- `reports/daily/YYYY-MM-DD_brief_review.md`

读取 `reports/daily/YYYY-MM-DD_brief_review.md`，按以下顺序向用户展示：

1. 当日事实。
2. 关键证据缺口。
3. 有效真实池。
4. 附录中的未确认系统候选；必须明确它们不是行动计划。
5. 待用户填写的判断。

不得使用网页搜索或模型常识补齐报告中的“待确认”。

## 四、收集人工判断

只记录用户明确表达的原话。可以依次询问：

- STEP 1：市场阶段。
- STEP 2：市场风格。
- STEP 3：情绪周期。
- STEP 4：核心板块；允许回答“不可判定”或“无”。
- STEP 5：主升板块及阶段；允许回答“不可判定”或“无”。
- STEP 8：预演对象、符合预期、超预期、不及预期和放弃条件。

不要替用户填写，不要把 Agent 建议改写成用户判断。准备写入前，先展示待写入字段、证据引用和将执行的完整 CLI 命令，再询问：`是否确认把以上人工判断写入本地复盘数据库？`

只有用户明确确认后，才能用 `.venv/Scripts/python.exe -m stock_review.cli` 调用：

```powershell
.\.venv\Scripts\python.exe -m stock_review.cli review record-step --date "YYYY-MM-DD" --step 1 --judgment "用户原话" --evidence-reference "data/evidence/YYYY-MM-DD_snapshot.json"
```

STEP 2-5 分别使用对应编号。没有用户答案的 STEP 不写入。

STEP 8 必须具有完整四类条件，并在用户确认后调用：

```powershell
.\.venv\Scripts\python.exe -m stock_review.cli review record-preview --date "YYYY-MM-DD" --target "用户确认的对象" --expectation "用户原话" --over-expectation "用户原话" --under-expectation "用户原话" --abandon-condition "用户原话" --evidence-reference "data/evidence/YYYY-MM-DD_snapshot.json" --confirmed
```

任一写入失败时停止并回显错误，不修改已有记录，不执行迁移。

## 五、Observation 单独确认

不得根据决策卡或 STEP 8 自动创建 Observation。只有用户明确要求创建，并逐项确认以下字段时才准备命令：

- 判断主题。
- 相关对象。
- 可验证假设。
- 成立条件。
- 失效条件。
- 证据来源。
- 最晚回填日期。

先逐字回显字段和完整命令，再单独询问：`是否确认创建这条 Observation？`

用户确认后才调用：

```powershell
.\.venv\Scripts\python.exe -m stock_review.cli observation add --date "YYYY-MM-DD" --topic "用户原话" --target "用户原话" --hypothesis "用户原话" --confirmation "用户原话" --invalidation "用户原话" --evidence-source "data/evidence/YYYY-MM-DD_snapshot.json" --review-deadline "YYYY-MM-DD"
```

若命令提示本地数据库尚未完成治理迁移，立即停止并说明需要用户对迁移另行明确授权；不得自行执行 `observation migrate-review-governance`。

## 六、收口输出

每次任务结束只汇报：

- 真实运行是否成功。
- 生成了哪些当日文件。
- 保存了哪些人工记录或 Observation ID。
- 哪些证据仍待确认。
- 哪些步骤未执行及原因。

不得把执行成功表述为市场判断正确，也不得把决策卡表述为投资建议。
