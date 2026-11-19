# 选择导出领域
#
# 用户双击「选择导出.bat」→ 方向键多选领域 → 确认导出位置 → 自动导出。
#
# ⚠️ 本文件必须保存为 UTF-8 with BOM + CRLF（原因见 choose-start.ps1 注释）。

param(
    [switch]$NoPause   # 测试用
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
Set-Location $Root

. (Join-Path $ScriptDir "domain-discovery.ps1")
. (Join-Path $ScriptDir "domain-menu-core.ps1")

Initialize-Console

function Find-Python {
    <#
    .SYNOPSIS
        找可用的 Python 解释器。

    .DESCRIPTION
        优先用项目虚拟环境（backend\.venv），其次用系统 PATH 里的 python。
        导出脚本只用到标准库，任何 3.9+ 都能跑。
    #>
    $venv = Join-Path $Root "backend\.venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }

    foreach ($cmd in @("python", "python3", "py")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    return $null
}

try {
    # ---- 1. 发现领域 ----
    $domains = Get-BusinessDomains -DomainRoot (Join-Path $Root "backend\app\domain")
    if ($domains.Count -eq 0) {
        Write-Host "  没有找到任何业务领域。" -ForegroundColor Red
        if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
        exit 1
    }

    # ---- 2. 多选 ----
    $picked = Show-DomainMenu -Domains $domains -Title "勾选要导出的领域（可多选）" -MultiSelect
    if ($picked.Count -eq 0) {
        Write-Host ""
        Write-Host "  已取消。" -ForegroundColor Yellow
        if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
        exit 0
    }

    # ---- 3. 选择导出位置 ----
    Clear-Host
    Write-Host ""
    Write-Host "  ╔══════════════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "  ║  选择导出位置                                        ║" -ForegroundColor Cyan
    Write-Host "  ╚══════════════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""
    Write-Host ("  将导出 " + $picked.Count + " 个领域：" + (($picked | ForEach-Object { $_.DisplayName }) -join "、")) -ForegroundColor White
    Write-Host ""
    Write-Host "  每个领域会生成一个独立项目，例如：" -ForegroundColor DarkGray
    foreach ($d in $picked) {
        $defaultName = "kbqa-" + $d.Name
        Write-Host ("    " + $d.DisplayName + "  ->  <导出位置>\" + $defaultName) -ForegroundColor DarkGray
    }
    Write-Host ""
    Write-Host "  直接回车 = 导出到项目旁边的文件夹（推荐）" -ForegroundColor Green
    Write-Host "  或输入父目录路径（如 D:\projects）" -ForegroundColor DarkGray
    Write-Host ""

    $defaultParent = Split-Path -Parent $Root
    Write-Host ("  导出位置 [" + $defaultParent + "]: ") -NoNewline -ForegroundColor Cyan
    $inputPath = Read-Host

    if ([string]::IsNullOrWhiteSpace($inputPath)) {
        $parent = $defaultParent
    } else {
        $parent = $inputPath.Trim().Trim('"')
    }

    if (-not (Test-Path $parent)) {
        Write-Host ""
        Write-Host ("  目录不存在：" + $parent) -ForegroundColor Red
        Write-Host "  请先创建该目录，或直接回车用默认位置。" -ForegroundColor DarkGray
        if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
        exit 1
    }

    # ---- 4. 找 Python ----
    $python = Find-Python
    if (-not $python) {
        Write-Host ""
        Write-Host "  找不到 Python 解释器，无法执行导出。" -ForegroundColor Red
        Write-Host "  请确认已安装 Python 3.9+ 并加入 PATH。" -ForegroundColor DarkGray
        if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
        exit 1
    }

    $extractScript = Join-Path $Root "scripts\extract.py"
    if (-not (Test-Path $extractScript)) {
        Write-Host ("  错误：找不到导出脚本 " + $extractScript) -ForegroundColor Red
        if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
        exit 1
    }

    # ---- 5. 逐个导出 ----
    Clear-Host
    Write-Host ""
    Write-Host "  开始导出..." -ForegroundColor Cyan
    Write-Host ""

    $results = @()
    foreach ($d in $picked) {
        $target = Join-Path $parent ("kbqa-" + $d.Name)

        Write-Host ("  [" + $d.DisplayName + "] -> " + $target) -ForegroundColor White
        Write-Host "  ────────────────────────────────────────────" -ForegroundColor DarkGray

        # 目标已存在时询问，避免误覆盖用户已改过的项目
        if (Test-Path $target) {
            Write-Host ("    目录已存在，继续将覆盖它。") -ForegroundColor Yellow
            Write-Host ("    确认覆盖？(y/N) ") -NoNewline -ForegroundColor Yellow
            $ans = Read-Host
            if ($ans -notmatch '^[Yy]') {
                Write-Host "    已跳过。" -ForegroundColor DarkGray
                $results += [PSCustomObject]@{ Domain = $d.DisplayName; Path = $target; Status = "跳过" }
                continue
            }
        }

        # 调用导出脚本；--force 用于覆盖已存在目录
        $args = @($extractScript, $d.Name, $target)
        if (Test-Path $target) { $args += "--force" }

        & $python @args 2>&1 | ForEach-Object {
            Write-Host ("    " + $_) -ForegroundColor DarkGray
        }

        if ($LASTEXITCODE -eq 0) {
            Write-Host "    ✅ 完成" -ForegroundColor Green
            $results += [PSCustomObject]@{ Domain = $d.DisplayName; Path = $target; Status = "成功" }
        } else {
            Write-Host "    ❌ 失败（退出码 $LASTEXITCODE）" -ForegroundColor Red
            $results += [PSCustomObject]@{ Domain = $d.DisplayName; Path = $target; Status = "失败" }
        }
        Write-Host ""
    }

    # ---- 6. 汇总 ----
    Write-Host "  ════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host "  导出结果" -ForegroundColor Cyan
    Write-Host "  ════════════════════════════════════════════════════" -ForegroundColor Cyan
    foreach ($r in $results) {
        $color = switch ($r.Status) {
            "成功" { "Green" }
            "跳过" { "DarkGray" }
            default { "Red" }
        }
        Write-Host ("    " + $r.Domain.PadRight(8) + $r.Status.PadRight(6) + $r.Path) -ForegroundColor $color
    }
    Write-Host ""

    $succeeded = @($results | Where-Object { $_.Status -eq "成功" })
    if ($succeeded.Count -gt 0) {
        Write-Host "  要不要现在打开导出的文件夹？(Y/n) " -NoNewline -ForegroundColor Cyan
        $open = Read-Host
        if ($open -notmatch '^[Nn]') {
            foreach ($r in $succeeded) {
                if (Test-Path $r.Path) { Start-Process explorer.exe $r.Path }
            }
        }
        Write-Host ""
        Write-Host "  提示：导出的项目是独立的，母项目的后续更新不会同步过去。" -ForegroundColor Yellow
        Write-Host "  详见导出项目里的 .exported-from 文件。" -ForegroundColor DarkGray
        Write-Host ""
    }
}
catch {
    Write-Host ""
    Write-Host "  导出失败：" -ForegroundColor Red
    Write-Host ("  " + $_.Exception.Message) -ForegroundColor Red
    Write-Host ""
}

if (-not $NoPause) {
    Read-Host "按回车关闭此窗口" | Out-Null
}
