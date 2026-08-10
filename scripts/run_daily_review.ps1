# 本脚本串联单个交易日的证据采集、有限候选历史和本地决策卡；LLM 详细复盘仅在显式启用时生成。

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

function Invoke-StockReviewStep {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $displayCommand = ".\.venv\Scripts\python.exe -m stock_review.cli " + ($Arguments -join " ")
    Write-Host ""
    Write-Host "[$Name]" -ForegroundColor Cyan
    Write-Host $displayCommand

    if ($DryRun) {
        return
    }

    & $pythonPath -m stock_review.cli @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "步骤失败：$Name（退出码 $LASTEXITCODE）。已停止，后续步骤未执行。"
    }
}

$previousPythonPath = $env:PYTHONPATH
$previousBytecodeSetting = $env:PYTHONDONTWRITEBYTECODE

Push-Location -LiteralPath $repositoryRoot
try {
    $env:PYTHONPATH = "src"
    $env:PYTHONDONTWRITEBYTECODE = "1"

    Invoke-StockReviewStep -Name "采集 AKShare 市场、情绪、板块和个股证据" -Arguments @(
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

    Invoke-StockReviewStep -Name "合并 hhxg 最近交易日情绪与板块证据" -Arguments @(
        "evidence", "collect-hhxg",
        "--date", $TradeDate,
        "--output-dir", "data/evidence",
        "--database", "data/stock_review.sqlite"
    )

    Invoke-StockReviewStep -Name "检查 Evidence Snapshot 缺口" -Arguments @(
        "evidence", "check",
        "--date", $TradeDate,
        "--snapshot-dir", "data/evidence"
    )

    Invoke-StockReviewStep -Name "采集有限复盘候选历史" -Arguments @(
        "evidence", "collect-review-history",
        "--date", $TradeDate,
        "--source", "akshare",
        "--snapshot-dir", "data/evidence",
        "--output-dir", "data/evidence",
        "--database", "data/stock_review.sqlite"
    )

    Invoke-StockReviewStep -Name "生成本地盘后复盘决策卡" -Arguments @(
        "review", "create-brief",
        "--date", $TradeDate,
        "--snapshot-dir", "data/evidence",
        "--database", "data/stock_review.sqlite",
        "--output-dir", "reports/daily"
    )

    if ($WithLlm) {
        Invoke-StockReviewStep -Name "生成 STEP 1-10 LLM 详细复盘" -Arguments @(
            "review", "create-detailed",
            "--date", $TradeDate,
            "--framework", "stock-review.md",
            "--snapshot-dir", "data/evidence",
            "--database", "data/stock_review.sqlite",
            "--output-dir", "reports/daily"
        )
    }

    Write-Host ""
    if ($DryRun) {
        Write-Host "DryRun 完成：未联网、未写文件、未调用 LLM。" -ForegroundColor Yellow
    }
    else {
        Write-Host "每日复盘完成。" -ForegroundColor Green
        Write-Host "快照：data/evidence/${TradeDate}_snapshot.json"
        Write-Host "候选历史：data/evidence/${TradeDate}_review_candidate_history.json"
        Write-Host "盘后复盘决策卡：reports/daily/${TradeDate}_brief_review.md"
        if ($WithLlm) {
            Write-Host "LLM 详细复盘：reports/daily/${TradeDate}_detailed_review.md"
        }
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
