# 方向键选择菜单（供启动/导出脚本共用）
#
# 为什么用 PowerShell 而不是批处理：
#   .bat 做不了方向键交互（只能用 set /p 让用户敲数字），
#   而 PowerShell 能直接读控制台的按键码、定位光标，可以画出真正的菜单。
#
# 为什么加 UTF-8 BOM：
#   Windows PowerShell 5.1 读**无 BOM** 的 UTF-8 文件时会按 GBK 解析，
#   中文注释会变成乱码，进而让解析器认为括号不配平、脚本直接报错。
#   这个坑真实踩过 —— 所以本文件必须带 BOM。

function Show-DomainMenu {
    <#
    .SYNOPSIS
        方向键选择领域，回车确认。

    .PARAMETER Domains
        Get-BusinessDomains 返回的领域数组。

    .PARAMETER Title
        菜单标题。

    .PARAMETER MultiSelect
        是否允许多选（空格勾选）。默认单选。

    .OUTPUTS
        选中的领域对象数组。用户按 Esc 时返回空数组。
    #>
    param(
        [Parameter(Mandatory = $true)]
        [array]$Domains,

        [string]$Title = "请选择业务领域",

        [switch]$MultiSelect
    )

    if ($Domains.Count -eq 0) {
        Write-Host "  没有找到任何领域。" -ForegroundColor Red
        return @()
    }

    $cursor = 0                      # 当前高亮项
    $checked = @{}                   # 多选时的勾选状态
    foreach ($d in $Domains) { $checked[$d.Name] = $false }

    # 单选时第一项默认高亮；多选时默认全不勾

    # 画菜单的函数（每次按键后重画）
    function DrawMenu {
        param($domains, $cursor, $checked, $multi)

        Clear-Host
        Write-Host ""
        Write-Host "  ╔══════════════════════════════════════════════════════╗" -ForegroundColor Cyan
        Write-Host "  ║  $Title" -ForegroundColor Cyan
        Write-Host "  ╚══════════════════════════════════════════════════════╝" -ForegroundColor Cyan
        Write-Host ""

        if ($multi) {
            Write-Host "    ↑↓ 移动    空格 勾选/取消    回车 确认    Esc 退出" -ForegroundColor DarkGray
        } else {
            Write-Host "    ↑↓ 移动    回车 确认    Esc 退出" -ForegroundColor DarkGray
        }
        Write-Host ""

        for ($i = 0; $i -lt $domains.Count; $i++) {
            $d = $domains[$i]
            $isCursor = ($i -eq $cursor)

            # 前缀：光标箭头 + 勾选框
            if ($multi) {
                $mark = if ($checked[$d.Name]) { "[✓]" } else { "[ ]" }
                $prefix = if ($isCursor) { "  ▸ $mark " } else { "    $mark " }
            } else {
                $prefix = if ($isCursor) { "  ▸ " } else { "    " }
            }

            $color = if ($isCursor) { "Black" } else { "Gray" }
            $bg = if ($isCursor) { "Cyan" } else { $null }

            if ($isCursor) {
                Write-Host $prefix -NoNewline -ForegroundColor Black -BackgroundColor Cyan
                Write-Host ("{0,-8}" -f $d.DisplayName) -NoNewline -ForegroundColor Black -BackgroundColor Cyan
                Write-Host ("  " + $d.AppTitle) -ForegroundColor Black -BackgroundColor Cyan
            } else {
                Write-Host $prefix -NoNewline -ForegroundColor Gray
                Write-Host ("{0,-8}" -f $d.DisplayName) -NoNewline -ForegroundColor White
                Write-Host ("  " + $d.AppTitle) -ForegroundColor DarkGray
            }

            # 高亮项下方显示示例问题，帮用户判断
            if ($isCursor -and $d.Examples.Count -gt 0) {
                Write-Host ("        例：" + $d.Examples[0]) -ForegroundColor DarkYellow
            }
            Write-Host ""
        }

        Write-Host "  ────────────────────────────────────────────────────────" -ForegroundColor DarkGray
        if ($multi) {
            $picked = @($domains | Where-Object { $checked[$_.Name] } | ForEach-Object { $_.DisplayName })
            if ($picked.Count -gt 0) {
                Write-Host ("  已选：" + ($picked -join "、")) -ForegroundColor Green
            } else {
                Write-Host "  已选：（尚未勾选，用空格勾选）" -ForegroundColor DarkGray
            }
        } else {
            Write-Host ("  当前选择：" + $domains[$cursor].DisplayName) -ForegroundColor Green
        }
        Write-Host ""
    }

    DrawMenu -domains $Domains -cursor $cursor -checked $checked -multi $MultiSelect

    # ---- 读按键循环 ----
    while ($true) {
        $key = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")

        switch ($key.VirtualKeyCode) {
            38 { # ↑
                if ($cursor -gt 0) { $cursor-- } else { $cursor = $Domains.Count - 1 }
                DrawMenu -domains $Domains -cursor $cursor -checked $checked -multi $MultiSelect
            }
            40 { # ↓
                if ($cursor -lt $Domains.Count - 1) { $cursor++ } else { $cursor = 0 }
                DrawMenu -domains $Domains -cursor $cursor -checked $checked -multi $MultiSelect
            }
            32 { # 空格：多选时切换勾选
                if ($MultiSelect) {
                    $name = $Domains[$cursor].Name
                    $checked[$name] = -not $checked[$name]
                    DrawMenu -domains $Domains -cursor $cursor -checked $checked -multi $MultiSelect
                }
            }
            13 { # 回车：确认
                if ($MultiSelect) {
                    $picked = @($Domains | Where-Object { $checked[$_.Name] })
                    if ($picked.Count -eq 0) {
                        # 一个都没勾，提示后继续（不退出，避免误按丢失操作）
                        Write-Host "  请至少勾选一个领域（空格勾选）。" -ForegroundColor Yellow
                        Start-Sleep -Milliseconds 1200
                        DrawMenu -domains $Domains -cursor $cursor -checked $checked -multi $MultiSelect
                        continue
                    }
                    return $picked
                } else {
                    return @($Domains[$cursor])
                }
            }
            27 { # Esc：退出
                return @()
            }
        }
    }
}
