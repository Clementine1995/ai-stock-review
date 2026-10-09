# 开发与验证流程

## 职责

本文件是本项目开发初始化、命令行自检、自动化测试、Git 操作和数据库人工作业的唯一可复制命令来源。`AGENTS.md` 定义授权与工程约束，`docs/PRODUCT_REQUIREMENTS.md` 定义产品与 CLI 行为合同，WorkBuddy Skill 定义机器编排参数；单项需求文档只引用本文件章节，不复制人工作业命令。

本文件所列人工作业命令变化只在这里修改。CLI 行为签名变化还必须同步产品合同、WorkBuddy Skill 和匹配测试。执行前仍须按当前任务核对工作区、目标数据库、联网范围和用户授权，不能用文档中的示例替代现场确认。

## 当前执行环境

- 操作系统：Windows。
- Shell：PowerShell。
- 唯一工作目录：`F:\codex-project\ai-stock-review`。
- Python：项目根目录 `.venv` 中的 Python 3.12 或更高版本。
- 项目形态：本地 CLI，不运行常驻服务。
- 自动化：当前没有 CI/CD；测试在本机执行。
- 发布：当前没有测试、预发布或生产环境发布流程。
- 正式业务库：`data\review_run.sqlite`。
- 历史旧库：`data\stock_review.sqlite`，不得作为新版日常命令的默认库。

除明确的真实数据源采集、正式模型调用和本地业务写入外，开发验证必须隔离外部网络和真实业务库。

## 开发前只读门禁

影响：只读检查，不修改文件、Git 历史或数据库。

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
Get-Location
git rev-parse --show-toplevel
git status --short --branch
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git diff --name-only
Test-Path -LiteralPath '.\.venv\Scripts\python.exe'
.\.venv\Scripts\python.exe --version
```

通过标准：工作目录解析为本仓库；当前分支和提交身份可读；已有未提交修改已经识别；虚拟环境 Python 版本不低于 3.12。存在未完成合并、工作区身份不明或当前任务会覆盖用户已有修改时停止。

本项目始终直接使用现有保存目录。未经用户在当前请求中明确要求，不创建额外工作区或 Git worktree。

## 初始化和依赖

首次创建虚拟环境或重建依赖会修改本地环境，必须先取得用户明确授权。项目没有独立构建步骤。

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
python -c "import sys; assert sys.version_info >= (3, 12), sys.version"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

成功标准：安装命令返回零，随后能执行“命令行自检”和“全量测试”。未经授权不得安装、升级或移除依赖，也不得修改 `.env` 或其它运行配置。

## 命令行自检

影响：只读加载 CLI，不联网、不写数据库。

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
$env:PYTHONPATH = 'src'
$env:PYTHONDONTWRITEBYTECODE = '1'
.\.venv\Scripts\python.exe -m stock_review.cli --help
.\.venv\Scripts\python.exe -m stock_review.cli framework check --file stock-review.md
```

通过标准：两个命令均返回零，CLI 能列出新版 `daily`、`observation`、`weekly` 和 `database` 命令，框架检查完整识别 STEP 1-10。

## 自动化测试

自动化测试只能使用测试创建的临时目录、临时 SQLite、fake HTTP server、fake model 或 Mock transport。测试不得连接真实数据源、调用正式模型、写入 `data\review_run.sqlite`、写入 `data\stock_review.sqlite`，也不得修改正式报告和日志。

### ReviewRun 定向回归

适用于 ReviewRun 状态机、证据、模型、发布质量和 renderer 改动。

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
$env:PYTHONPATH = 'src'
$env:PYTHONDONTWRITEBYTECODE = '1'
.\.venv\Scripts\python.exe -m unittest tests.test_review_run_domain tests.test_review_run_collection tests.test_complete_daily_review tests.test_complete_model_review tests.test_review_run_actionable_observation tests.test_finalize_daily_review tests.test_review_run_renderer tests.test_review_run_cli_e2e
```

### WorkBuddy 合同回归

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
$env:PYTHONPATH = 'src'
$env:PYTHONDONTWRITEBYTECODE = '1'
.\.venv\Scripts\python.exe -m unittest tests.test_workbuddy_skill
```

### 全量测试

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
$env:PYTHONPATH = 'src'
$env:PYTHONDONTWRITEBYTECODE = '1'
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

### 差异完整性

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
git diff --check
git status --short
```

每个需求按直接影响选择定向测试，再决定是否运行全量测试。测试结果必须来自当前工作区和当前命令，历史通过数字不能替代本次验证。项目当前没有独立 lint、类型检查、打包或 CI 命令，不为形式完整虚构检查。

## Git 操作

### 只读核查

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
git status --short --branch
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git diff --name-only
git diff
git log -5 --oneline
```

### 远端同步

`git fetch` 和当前分支对齐会改变本地 Git 状态，必须先取得用户对本次远端同步的明确授权。纯只读核查和普通本地修改不自动执行远端同步。

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
$branchName = git branch --show-current
if ([string]::IsNullOrWhiteSpace($branchName)) { throw '当前不在可同步的本地分支上。' }
git fetch origin
if ($LASTEXITCODE -ne 0) { throw '远端引用同步失败。' }
git merge --ff-only ('origin/' + $branchName)
```

同步前必须确认没有未完成合并，且用户已有修改不会与远端变化重叠。非快进、冲突、远端分支不存在或执行结果不明时停止，不改用 `git pull`、rebase 或强制操作绕过现场。

### 暂存和提交

`git add` 与 `git commit` 均须用户明确授权。只选择当前任务修改；交互式暂存后必须检查暂存区，发现无关文件立即停止提交。

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
git add -i
git diff --cached --check
git diff --cached --name-only
git diff --cached
git commit
```

提交信息使用中文，前缀只允许 `feat:`、`fix:`、`docs:`、`refactor:`、`test:` 或 `chore:`。

### 推送

推送会改变远端代码线，必须在提交之后另行取得明确授权。

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
git status --short --branch
git log -1 --oneline
git push origin HEAD
```

禁止强推。禁止用 `git reset`、`git restore`、`git checkout`、`git revert` 或回滚脚本自行修复流程错误；发生误操作时保护现场并报告当前分支、HEAD、工作区状态和已执行命令。

## 数据库操作

### 数据库边界

- `data\review_run.sqlite` 是新版正式本地业务库。日常写命令只有在用户确认对应 RunManifest、模型请求、FinalizationProposal 或结果 action 后才能执行。
- `data\stock_review.sqlite` 是历史旧库。新版日常命令不得指向它，是否原地迁移属于独立任务。
- 自动化测试必须使用临时 SQLite，不得复用以上两个数据库。
- 任何数据库只读核查前都要先说明环境为本地、具体库名和查询范围；只允许 `SELECT` 或只读 pragma。
- 数据库写入、迁移、数据修复和删除必须分别取得明确授权，不能从代码修改、测试或日常复盘授权中推定。

### 只读身份与完整性检查

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
$databasePaths = @('data\review_run.sqlite', 'data\stock_review.sqlite')
foreach ($databasePath in $databasePaths) {
    if (Test-Path -LiteralPath $databasePath) {
        Get-Item -LiteralPath $databasePath | Select-Object FullName,Length,LastWriteTimeUtc
        Get-FileHash -LiteralPath $databasePath -Algorithm SHA256
        .\.venv\Scripts\python.exe -c "import sqlite3, sys; from pathlib import Path; uri = Path(sys.argv[1]).resolve().as_uri() + '?mode=ro'; connection = sqlite3.connect(uri, uri=True); print(sys.argv[1], 'application_id=', connection.execute('PRAGMA application_id').fetchone()[0], 'user_version=', connection.execute('PRAGMA user_version').fetchone()[0], 'integrity_check=', connection.execute('PRAGMA integrity_check').fetchone()[0]); connection.close()" $databasePath
    } else {
        Write-Output ($databasePath + ' does not exist')
    }
}
```

目标文件不存在时如实记录，不创建空库冒充现状。新版库只允许在获确认 RunManifest 后由首次 `daily start` 初始化。

## 数据库迁移

### 生成旧库迁移计划

本流程只对 `data\stock_review.sqlite` 做原地 schema version 1 迁移和 legacy 登记，不会把旧库合并进 `data\review_run.sqlite`。生成计划对数据库只读，但会在操作系统临时目录写入一份待确认 JSON；只有用户已经要求评估或准备迁移计划时才允许这项临时文件影响。此时不会创建备份或修改数据库。

```powershell
Set-Location -LiteralPath 'F:\codex-project\ai-stock-review'
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$migrationPlanPath = Join-Path ([System.IO.Path]::GetTempPath()) ('ai-stock-review-migration-plan-' + $timestamp + '.json')
$backupPath = Join-Path ([Environment]::GetFolderPath('MyDocuments')) ('ai-stock-review-backups\stock_review-' + $timestamp + '.sqlite')
[string]$planJson = & .\.venv\Scripts\python.exe -m stock_review.cli database plan-migration --database 'data\stock_review.sqlite' --backup-path $backupPath --target-version 1 --evidence-dir 'data\evidence' --daily-report-dir 'reports\daily' --weekly-report-dir 'reports\weekly'
if ($LASTEXITCODE -ne 0) { throw '迁移计划生成失败。' }
[System.IO.File]::WriteAllText($migrationPlanPath, $planJson, [System.Text.UTF8Encoding]::new($false))
$migrationPlan = $planJson | ConvertFrom-Json
$planJson
$migrationPlan.content_hash
$migrationPlanPath
```

必须向用户完整展示同一份计划正文、`content_hash`、目标库、目标版本、备份路径、数据库逻辑快照指纹、旧表和历史文件清单、影响范围及失败处理。计划中的 `database_sha256` 是同一只读事务内对完整 schema、全部表记录、`application_id` 和 `user_version` 计算的逻辑快照指纹，包含已提交但尚未 checkpoint 的 WAL 页，不是主文件字节哈希。

计划正文变化、数据库或历史文件变化、计划文件丢失、备份路径被占用或用户尚未针对该计划哈希明确确认时，不得迁移。必须重新生成完整计划并重新确认，不能只替换哈希。

下一节命令必须在保留上述变量的同一 PowerShell 会话中执行。会话中断或变量来源不确定时，原确认失效；重新生成、完整展示并确认新计划，不从目录中猜测某个计划文件继续执行。

### 执行已确认迁移

影响：写入 `data\stock_review.sqlite`，通过 SQLite Online Backup 在仓库外创建备份，登记 legacy 记录并追加审计。必须取得针对上一节完整计划和同一 `content_hash` 的单独明确授权后执行。

```powershell
[string]$confirmedPlanJson = [System.IO.File]::ReadAllText($migrationPlanPath, [System.Text.Encoding]::UTF8)
$confirmedPlan = $confirmedPlanJson | ConvertFrom-Json
$confirmedPlanBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($confirmedPlanJson))
$confirmedPlanBase64 | .\.venv\Scripts\python.exe -m stock_review.cli database migrate --database 'data\stock_review.sqlite' --plan-base64-stdin --plan-hash $confirmedPlan.content_hash --confirmed --audit-log 'logs\review_run.log'
if ($LASTEXITCODE -ne 0) { throw '迁移未成功完成，保留现场并进入只读核查。' }
```

迁移命令会先取得 SQLite `BEGIN IMMEDIATE` 单写者门禁，再校验目标库、完整计划、计划哈希和逻辑快照指纹；门禁内使用 SQLite Online Backup，并用 `PRAGMA integrity_check` 和同一逻辑指纹验证备份。旧表重命名、新 schema、legacy 登记和迁移审计在同一事务提交。成功后保存命令回执、计划文件、计划哈希、备份路径和审计事件，再进行只读回查。

不得手写 DDL/DML 替代该流程，不得把备份文件复制回主库作为自动恢复，也不得删除主库的 WAL/SHM sidecar。数据修复或回填必须先形成单独需求合同，列出目标表、筛选条件、预计数量、幂等性、回查 SQL 和失败处理，再取得独立授权。

## 超时、中断和未知现场

Git 同步、真实数据源、正式模型、数据库写入或迁移出现超时、断连、进程中断或结果未知时：

1. 停止重试和后续写操作。
2. 记录最后一个确认成功的步骤、完整命令、时间、分支、HEAD、数据库路径、业务 ID 和哈希。
3. 只读执行本文件的“开发前只读门禁”“Git 只读核查”或“数据库只读身份与完整性检查”，并读取已有 CLI 回执、审计日志和数据库投影。
4. 区分未执行、执行中断、已生效但响应丢失和失败后部分生效。
5. 基于现场事实提出继续、幂等补验或人工恢复方案；未经确认不删除、不覆盖、不回滚、不盲目重跑。

## 不适用能力

本项目当前没有常驻服务、服务启停命令、容器、CI/CD、制品发布、多环境晋级或生产发布流程。新增这些能力前必须形成独立需求和授权，并先更新本文件；其它文档不得自行提供一套可执行命令。
