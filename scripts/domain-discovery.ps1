# 领域发现（供菜单脚本调用）
#
# 作用：扫描 backend/app/domain/ 下所有领域目录，读出展示信息。
#
# 为什么不把领域列表写死在菜单里：
#   写死的话，以后新增医疗、法律等领域时要改菜单脚本，
#   改漏了菜单里就没有新领域。扫描目录则新增即出现，零维护。

function Get-BusinessDomains {
    <#
    .SYNOPSIS
        返回所有可用领域的信息。

    .DESCRIPTION
        扫描 backend/app/domain/ 下的子目录，凡是含 domain.py 的算一个领域。
        读取 domain.py 里的 display_name、app_title、persona、example_questions
        用于菜单展示。

        用正则从源码里抠字段，而不是 import Python 模块 ——
        后者需要启动解释器，菜单打开会慢 1~2 秒，体验差。

    .OUTPUTS
        PSCustomObject 数组，每项含：
            Name        领域标识（目录名，如 ecommerce）
            DisplayName 中文名（如 电商）
            AppTitle    应用标题
            Persona     系统身份
            Examples    示例问题（数组，可能为空）
    #>
    param(
        [string]$DomainRoot
    )

    if (-not (Test-Path $DomainRoot)) {
        Write-Host "错误：找不到领域目录 $DomainRoot" -ForegroundColor Red
        return @()
    }

    $domains = @()

    Get-ChildItem -Path $DomainRoot -Directory | ForEach-Object {
        $domainPy = Join-Path $_.FullName "domain.py"
        if (-not (Test-Path $domainPy)) {
            return  # 跳过：没有 domain.py 的目录不是领域
        }

        $content = Get-Content -Path $domainPy -Raw -Encoding UTF8

        # ---- display_name="电商" ----
        $displayName = $_.Name
        $m = [regex]::Match($content, 'display_name\s*=\s*"([^"]+)"')
        if ($m.Success) { $displayName = $m.Groups[1].Value }

        # ---- app_title="商品知识库问答系统" ----
        $appTitle = ""
        $m = [regex]::Match($content, 'app_title\s*=\s*"([^"]+)"')
        if ($m.Success) { $appTitle = $m.Groups[1].Value }

        # ---- persona="你是电商..." ----
        $persona = ""
        $m = [regex]::Match($content, 'persona\s*=\s*"([^"]+)"')
        if ($m.Success) { $persona = $m.Groups[1].Value }

        # ---- example_questions=( "A8 适用多大面积", ... ) ----
        $examples = @()
        $m = [regex]::Match($content, 'example_questions\s*=\s*\(([\s\S]*?)\)')
        if ($m.Success) {
            $block = $m.Groups[1].Value
            [regex]::Matches($block, '"([^"]+)"') | ForEach-Object {
                $examples += $_.Groups[1].Value
            }
        }

        $domains += [PSCustomObject]@{
            Name        = $_.Name
            DisplayName = $displayName
            AppTitle    = $appTitle
            Persona     = $persona
            Examples    = $examples
        }
    }

    # 排序：ecommerce、finance 固定在前，其余按名字排（generic 垫底）
    $order = @{ 'ecommerce' = 0; 'finance' = 1 }
    $domains | Sort-Object -Property @{
        Expression = {
            if ($order.ContainsKey($_.Name)) { $order[$_.Name] }
            elseif ($_.Name -eq 'generic') { 99 }
            else { 50 }
        }
    }, DisplayName
}


function Initialize-Console {
    <#
    .SYNOPSIS
        统一控制台编码，避免中文乱码。

    .DESCRIPTION
        Windows 控制台默认编码可能是 GBK 或 936，
        而脚本文件是 UTF-8，不设置会出现"选择领域"变乱码的情况。
    #>
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        $OutputEncoding = [System.Text.Encoding]::UTF8
    } catch {
        # 某些终端不支持设置编码，忽略即可（不影响功能）
    }
}
