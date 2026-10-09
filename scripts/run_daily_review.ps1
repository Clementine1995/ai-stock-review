# 本脚本串联单个交易日的证据采集、有限候选历史和本地决策卡；LLM 详细复盘仅在显式启用时生成。

# 过渡入口：本脚本不能生成新版正式 publication，日常入口使用 ReviewRun daily 命令。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TradeDate,

    [switch]$DryRun,

    [switch]$WithLlm
)

$ErrorActionPreference = "Stop"

try {
    [void][datetime]::ParseExact(
        $TradeDate,
        "yyyy-MM-dd",
        [System.Globalization.CultureInfo]::InvariantCulture
    )
}
catch {
    throw "TradeDate 必须是有效的 YYYY-MM-DD 日期：$TradeDate"
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $repositoryRoot ".venv\Scripts\python.exe"

if (-not $DryRun -and -not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "未找到项目虚拟环境 Python：$pythonPath"
}

# 命令展示与真实执行都从同一参数数组生成，避免权限预览和实际行为漂移。
function Format-StockReviewCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    return ".\.venv\Scripts\python.exe -m stock_review.cli " + ($Arguments -join " ")
}


# 正式执行只接收已经进入权限清单的步骤参数。
function Invoke-StockReviewStep {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $displayCommand = Format-StockReviewCommand -Arguments $Arguments
    Write-Host ""
    Write-Host "[$Name]" -ForegroundColor Cyan
    Write-Host $displayCommand

    & $pythonPath -m stock_review.cli @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "步骤失败：$Name（退出码 $LASTEXITCODE）。已停止，后续步骤未执行。"
    }
}


# DryRun 通过 stdout 输出单行 JSON，供 WorkBuddy 完整展示并取得用户确认。
function Write-PermissionManifest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ReviewDate,

        [Parameter(Mandatory = $true)]
        [object[]]$Steps,

        [Parameter(Mandatory = $true)]
        [bool]$IncludesLlm
    )

    $manifestSteps = @(
        foreach ($step in $Steps) {
            [ordered]@{
                name = $step.Name
                command = Format-StockReviewCommand -Arguments $step.Arguments
                arguments = @($step.Arguments)
                network_sources = @($step.NetworkSources)
                file_writes = @($step.FileWrites)
                database_writes = @($step.DatabaseWrites)
            }
        }
    )

    $manifest = [ordered]@{
        schema_version = 1
        kind = "stock_review.daily_review.permission_manifest"
        dry_run = $true
        executes_commands = $false
        trade_date = $ReviewDate
        includes_llm = $IncludesLlm
        network_sources = @(
            $Steps |
                ForEach-Object { $_.NetworkSources } |
                Where-Object { $_ } |
                Select-Object -Unique
        )
        file_writes = @(
            $Steps |
                ForEach-Object { $_.FileWrites } |
                Where-Object { $_ } |
                Select-Object -Unique
        )
        database_writes = @(
            $Steps |
                ForEach-Object { $_.DatabaseWrites } |
                Where-Object { $_ } |
                Select-Object -Unique
        )
        steps = $manifestSteps
    }

    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    Write-Output ($manifest | ConvertTo-Json -Depth 8 -Compress)
}


# 步骤定义同时驱动 DryRun 权限清单和真实执行。
$reviewSteps = @(
    [pscustomobject][ordered]@{
        Name = "采集 AKShare 市场、情绪、板块和个股证据"
        Arguments = @(
            "evidence", "collect-daily",
            "--date", $TradeDate,
            "--source", "akshare",
            "--scope", "market",
            "--scope", "sentiment",
            "--scope", "sectors",
            "--scope", "stocks",
            "--output-dir", "data/evidence",
            "--database", "data/stock_review.sqlite"
        )
        NetworkSources = @("akshare")
        FileWrites = @("data/evidence/${TradeDate}_snapshot.json", "logs/stock_review.log")
        DatabaseWrites = @("data/stock_review.sqlite")
    },
    [pscustomobject][ordered]@{
        Name = "合并 hhxg 最近交易日情绪与板块证据"
        Arguments = @(
            "evidence", "collect-hhxg",
            "--date", $TradeDate,
            "--output-dir", "data/evidence",
            "--database", "data/stock_review.sqlite"
        )
        NetworkSources = @("hhxg")
        FileWrites = @("data/evidence/${TradeDate}_snapshot.json", "logs/stock_review.log")
        DatabaseWrites = @("data/stock_review.sqlite")
    },
    [pscustomobject][ordered]@{
        Name = "检查 Evidence Snapshot 缺口"
        Arguments = @(
            "evidence", "check",
            "--date", $TradeDate,
            "--snapshot-dir", "data/evidence"
        )
        NetworkSources = @()
        FileWrites = @()
        DatabaseWrites = @()
    },
    [pscustomobject][ordered]@{
        Name = "采集有限复盘候选历史"
        Arguments = @(
            "evidence", "collect-review-history",
            "--date", $TradeDate,
            "--source", "akshare",
            "--snapshot-dir", "data/evidence",
            "--output-dir", "data/evidence",
            "--database", "data/stock_review.sqlite"
        )
        NetworkSources = @("akshare")
        FileWrites = @(
            "data/evidence/${TradeDate}_review_candidate_history.json",
            "logs/stock_review.log"
        )
        DatabaseWrites = @()
    },
    [pscustomobject][ordered]@{
        Name = "生成本地盘后复盘决策卡"
        Arguments = @(
            "review", "create-brief",
            "--date", $TradeDate,
            "--snapshot-dir", "data/evidence",
            "--database", "data/stock_review.sqlite",
            "--output-dir", "reports/daily"
        )
        NetworkSources = @()
        FileWrites = @("reports/daily/${TradeDate}_brief_review.md", "logs/stock_review.log")
        DatabaseWrites = @()
    }
)

if ($WithLlm) {
    $reviewSteps += [pscustomobject][ordered]@{
        Name = "生成 STEP 1-10 LLM 详细复盘"
        Arguments = @(
            "review", "create-detailed",
            "--date", $TradeDate,
            "--framework", "stock-review.md",
            "--snapshot-dir", "data/evidence",
            "--database", "data/stock_review.sqlite",
            "--output-dir", "reports/daily"
        )
        NetworkSources = @("openai-compatible-llm")
        FileWrites = @("reports/daily/${TradeDate}_detailed_review.md", "logs/stock_review.log")
        DatabaseWrites = @()
    }
}

if ($DryRun) {
    Write-PermissionManifest -ReviewDate $TradeDate -Steps $reviewSteps -IncludesLlm ([bool]$WithLlm)
    return
}

$previousPythonPath = $env:PYTHONPATH
$previousBytecodeSetting = $env:PYTHONDONTWRITEBYTECODE

Push-Location -LiteralPath $repositoryRoot
try {
    $env:PYTHONPATH = "src"
    $env:PYTHONDONTWRITEBYTECODE = "1"

    foreach ($step in $reviewSteps) {
        Invoke-StockReviewStep -Name $step.Name -Arguments $step.Arguments
    }

    Write-Host ""
    Write-Host "每日复盘完成。" -ForegroundColor Green
    Write-Host "快照：data/evidence/${TradeDate}_snapshot.json"
    Write-Host "候选历史：data/evidence/${TradeDate}_review_candidate_history.json"
    Write-Host "盘后复盘决策卡：reports/daily/${TradeDate}_brief_review.md"
    if ($WithLlm) {
        Write-Host "LLM 详细复盘：reports/daily/${TradeDate}_detailed_review.md"
    }
}
finally {
    Pop-Location
    if ($null -eq $previousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $previousPythonPath
    }
    if ($null -eq $previousBytecodeSetting) {
        Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONDONTWRITEBYTECODE = $previousBytecodeSetting
    }
}
