# 本地数据与验证说明

## 职责

本文件说明本地开发环境中的业务数据、验证样例、报告和日志如何区分，以及离线验证、备份、恢复和清理时必须遵守的边界。

## 数据分类

| 类型 | 当前路径或对象 | 口径 |
| --- | --- | --- |
| 离线输入样例 | `data/evidence/2026-07-06_sample.json` | 只用于无网络测试，不代表真实行情 |
| 真实采集快照 | `data/evidence/YYYY-MM-DD_snapshot.json` | 记录数据来源、样本日期和缺口；历史文件不代表当前行情 |
| 本地业务库 | `data/stock_review.sqlite` | 保存 Evidence Snapshot、池子及其多板块关联、人工 STEP 判断、STEP 8 预演、Observation 和回填记录 |
| 日报与计划 | `reports/daily/` | 由明确交易日期、框架和证据生成；是否为真实复盘取决于人工内容和证据完整度 |
| 周度学习 | `reports/weekly/` | 只汇总已保存 Observation；验收样例不代表真实经验 |
| 本地日志 | `logs/stock_review.log` | 记录正式 CLI 写操作及其对象、日期、输出和状态 |

当前 `2026-07-10` 日报、计划、周报和 `OBS-20260710-001` 是 M8 系统验收样例。该 Observation 状态为 `invalid`，不进入经验候选。它们证明链路可运行，不证明真实复盘效果已经可用。

## 离线最小验证

在仓库根目录执行：

```powershell
$env:PYTHONPATH='src'
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m stock_review.cli framework check --file stock-review.md
.\.venv\Scripts\python.exe -m stock_review.cli evidence check --date 2026-07-06
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

预期结果：

- 框架检查识别当前 `stock-review.md` 的全部 STEP。
- 证据检查显示来源、样本日期和明确缺口。
- 自动化测试全部通过。

离线验证不得调用真实数据源，不需要修改 `.env`，也不写入生产或远端状态。

## 备份

备份对象至少包括：

- `data/stock_review.sqlite`
- 需要保留的 `data/evidence/`
- 需要保留的 `reports/`
- `logs/stock_review.log`
- 用户维护的 `stock-review.md`

备份目录必须位于仓库外，并使用日期时间命名。执行前先确认目标目录不存在或为空，避免把新旧备份混在一起。备份属于文件复制，不改变当前业务库内容。

示例命令中的目标路径必须由用户替换为明确的仓库外目录：

```powershell
$backupRoot='D:\backup\ai-stock-review\2026-07-11-190000'
New-Item -ItemType Directory -Path $backupRoot
Copy-Item -LiteralPath data\stock_review.sqlite -Destination $backupRoot
Copy-Item -LiteralPath data\evidence -Destination $backupRoot -Recurse
Copy-Item -LiteralPath reports -Destination $backupRoot -Recurse
Copy-Item -LiteralPath logs\stock_review.log -Destination $backupRoot
Copy-Item -LiteralPath stock-review.md -Destination $backupRoot
```

## 恢复

恢复会覆盖当前状态，默认禁止自动执行。恢复前必须：

1. 明确目标环境为本地开发环境。
2. 停止所有可能写入本地数据库或报告的 CLI 进程。
3. 只读确认备份目录、文件日期和 SQLite 表结构。
4. 先为当前状态创建独立备份。
5. 明确获得用户对覆盖目标文件的授权。

恢复后至少运行框架检查、证据检查、SQLite 只读回查和自动化测试。若任一检查失败，停止使用恢复后的数据，不执行 Git 回滚命令或数据库回滚脚本。

## 清理

数据库、证据快照、报告和日志都可能包含历史复盘证据，默认不得删除。清理前必须逐项确认：

- 文件或记录属于离线样例、系统验收样例还是真实业务数据。
- 是否已完成仓库外备份。
- 删除条件、目标路径或 SQL 筛选条件及预计影响数量。
- 删除后的回查方式和失败处理方式。

清理操作必须单独获得用户明确授权。数据库记录不得通过手工编辑 SQLite 文件清理；需要删除或迁移时，应先设计正式命令、回查 SQL 和测试。

## 非目标

- 不把 Git 当作 SQLite、日志或用户复盘记录的备份系统。
- 不在本地验证过程中调用生产服务或写远端数据。
- 不把 M8 验收样例当作真实交易结论或学习样本。
