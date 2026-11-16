# 选择启动领域
#
# 用户双击「选择启动.bat」→ 运行本脚本 → 方向键选择 → 自动启动服务。
#
# ⚠️ 本文件必须保存为 UTF-8 with BOM + CRLF。
#    Windows PowerShell 5.1 读无 BOM 的 UTF-8 会按 GBK 解析，
#    中文注释变乱码后会让解析器认为括号不配平、脚本直接报错。

param(
    [switch]$NoPause   # 测试用：结束后不停顿
)

$ErrorActionPreference = "Stop"

# ---- 定位项目根目录 ----
# 本脚本在 <根>/scripts/ 下，所以上级目录就是根。
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
Set-Location $Root

. (Join-Path $ScriptDir "domain-discovery.ps1")
. (Join-Path $ScriptDir "domain-menu-core.ps1")

Initialize-Console

try {
    # ---- 1. 发现领域 ----
    $domains = Get-BusinessDomains -DomainRoot (Join-Path $Root "backend\app\domain")
    if ($domains.Count -eq 0) {
        Write-Host ""
        Write-Host "  没有找到任何业务领域，无法启动。" -ForegroundColor Red
        Write-Host "  请确认 backend\app\domain\ 下有领域目录。" -ForegroundColor DarkGray
        Write-Host ""
        if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
        exit 1
    }

    # ---- 2. 选择 ----
    $picked = Show-DomainMenu -Domains $domains -Title "选择要启动的业务领域"
    if ($picked.Count -eq 0) {
        Write-Host ""
        Write-Host "  已取消。" -ForegroundColor Yellow
        Write-Host ""
        if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
        exit 0
    }

    $domain = $picked[0]

    # ---- 3. 确认 ----
    Clear-Host
    Write-Host ""
    Write-Host "  ┌────────────────────────────────────────────────────┐" -ForegroundColor Green
    Write-Host "  │  准备启动                                          │" -ForegroundColor Green
    Write-Host "  ├────────────────────────────────────────────────────┤" -ForegroundColor Green
    Write-Host ("  │  领域：" + $domain.DisplayName) -ForegroundColor Green
    Write-Host ("  │  标识：" + $domain.Name) -ForegroundColor Green
    Write-Host ("  │  系统：" + $domain.AppTitle) -ForegroundColor Green
    Write-Host "  └────────────────────────────────────────────────────┘" -ForegroundColor Green
    Write-Host ""
    Write-Host "  正在启动，请稍候（首次启动可能需要 30 秒）..." -ForegroundColor Cyan
    Write-Host ""

    # ---- 4. 设置环境变量并调用现有启动脚本 ----
    # 现有 启动.bat 已经支持读 DOMAIN 环境变量（见该文件第 34 行附近），
    # 所以这里只要把它设好，启动脚本就会加载对应领域。
    $env:DOMAIN = $domain.Name

    $launcher = Join-Path $Root "启动.bat"
    if (-not (Test-Path $launcher)) {
        Write-Host "  错误：找不到启动脚本 $launcher" -ForegroundColor Red
        if (-not $NoPause) { Read-Host "按回车退出" | Out-Null }
        exit 1
    }

    # 用 cmd 调用，保证 .bat 在正确的 shell 里执行
    & cmd.exe /c "`"$launcher`""

    # ---- 5. 完成提示 ----
    Write-Host ""
    Write-Host "  ✅ 已用「$($domain.DisplayName)」领域启动" -ForegroundColor Green
    Write-Host ""
    Write-Host "  访问地址（端口以 .env 为准，默认 5173）：" -ForegroundColor DarkGray
    Write-Host "    http://127.0.0.1:5173" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  关闭服务：双击「停止.bat」" -ForegroundColor DarkGray
    Write-Host ""
}
catch {
    Write-Host ""
    Write-Host "  启动失败：" -ForegroundColor Red
    Write-Host ("  " + $_.Exception.Message) -ForegroundColor Red
    Write-Host ""
}

if (-not $NoPause) {
    Read-Host "按回车关闭此窗口" | Out-Null
}
