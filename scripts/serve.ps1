# 启动 / 停止 ui-locate 常驻视觉定位服务
#
# 用法（在插件根目录下执行）：
#   pwsh -File .\scripts\serve.ps1
#   pwsh -File .\scripts\serve.ps1 -Stop
#   pwsh -File .\scripts\serve.ps1 -Status
#
# 注意：正常使用不必手动跑这个脚本 —— MCP 工具会自己把服务拉起来。
# 这里只是给「想单独起、单独停、单独看状态」的场合用。
#
# 关于控制台窗口（踩过的坑）：
#   以前用 python.exe + -WindowStyle Hidden 起服务，窗口是看不见了，但
#   python.exe 是 Console 子系统程序，Windows 一定会给它分配控制台 ——
#   conhost.exe 照样挂在进程上，任务管理器里能看见，用户也会觉得「有个
#   CMD 窗口常驻」。真正的解法是 pythonw.exe（Windows 子系统，不分配
#   控制台），本脚本已改用它。

param(
    [switch]$Stop,
    [switch]$Status,
    [int]$Port = 0
)

$ErrorActionPreference = 'Stop'

# 插件根目录 = 本脚本上一级。不写死路径，插件放到哪都能用。
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root '.cache'
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
$py  = Join-Path $root '.venv\Scripts\python.exe'

# 端口优先级：命令行 -Port > 环境变量 UI_LOCATE_PORT > 默认 7788
if ($Port -le 0) {
    $Port = if ($env:UI_LOCATE_PORT) { [int]$env:UI_LOCATE_PORT } else { 7788 }
}

function Get-LocateProcs {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like '*ui_locate.server*' })
}

function Test-Port([int]$p) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { $c.Connect('127.0.0.1', $p); return $true }
    catch { return $false }
    finally { $c.Dispose() }
}

function Show-Status {
    $procs = Get-LocateProcs
    if ($procs.Count -eq 0) {
        Write-Output 'service: not running'
        return
    }
    foreach ($p in $procs) {
        $ram = [math]::Round($p.WorkingSetSize / 1MB, 0)
        Write-Output ("service: pid={0} exe={1} ram={2}MB" -f $p.ProcessId, $p.Name, $ram)
        # 有没有挂控制台 —— 这就是那个"CMD 窗口常驻"问题的判据
        $conhost = @(Get-CimInstance Win32_Process -Filter "Name='conhost.exe'" |
            Where-Object { $_.ParentProcessId -eq $p.ProcessId })
        if ($conhost.Count -gt 0) {
            Write-Output ("  conhost: {0} 个（不该有；说明还在用 python.exe）" -f $conhost.Count)
        }
        else {
            Write-Output '  conhost: 无'
        }
    }
    if (Test-Port $Port) {
        try {
            $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 4
            Write-Output ("health : ready={0} provider={1} warmup={2:N1}s" -f `
                    $h.ready, $h.active, ($h.warmup_ms / 1000))
            if ($h.screen) {
                Write-Output ("screen : dpi={0} virtual={1}x{2}@{3},{4} monitors={5}" -f `
                        $h.screen.dpi_awareness, $h.screen.virtual.w, $h.screen.virtual.h, `
                        $h.screen.virtual.x, $h.screen.virtual.y, $h.screen.monitors.Count)
                foreach ($m in $h.screen.monitors) {
                    Write-Output ("         [{0}] {1} {2}x{3} at ({4},{5}){6}" -f `
                            $m.index, $m.device, $m.w, $m.h, $m.x, $m.y, `
                            $(if ($m.primary) { ' primary' } else { '' }))
                }
            }
        }
        catch { Write-Output 'health : 端口通但 /health 无响应' }
    }
    else { Write-Output "port   : $Port 未监听" }
}

if ($Status) { Show-Status; return }

if ($Stop) {
    $procs = Get-LocateProcs
    if ($procs.Count -eq 0) { Write-Output 'no running server'; return }
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Write-Output ("stopped pid={0} ({1})" -f $p.ProcessId, $p.Name)
        }
        catch { Write-Output ("skip pid={0}: already gone" -f $p.ProcessId) }
    }
    return
}

# 端口有人占着就绝不重复拉起第二个
if (Test-Port $Port) {
    for ($i = 0; $i -lt 5; $i++) {
        try {
            $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 4
            Write-Output ("already running: provider={0} warmup={1:N1}s" -f `
                    $h.active, ($h.warmup_ms / 1000))
            return
        }
        catch { Start-Sleep -Milliseconds 800 }
    }
    Write-Output "port $Port occupied but /health does not answer; not spawning a duplicate"
    return
}

if (-not (Test-Path $pyw)) {
    Write-Output "missing $pyw"
    Write-Output "pythonw.exe 用来避免弹控制台。若确实没有，把本脚本里的 `$pyw 改成 `$py"
    return
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$outLog = Join-Path $logDir 'server.log'
$errLog = Join-Path $logDir 'server.err.log'

# 用 pythonw.exe：Windows 子系统程序，不分配控制台，不会挂 conhost。
# 重定向仍然要留着 —— pythonw 的 stdout 默认不可见，不打日志就没法排查。
$p = Start-Process -FilePath $pyw `
    -ArgumentList @('-m', 'ui_locate.server', '--port', "$Port") `
    -WorkingDirectory $root `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog

Write-Output ("spawned pid={0} (pythonw, no console), warming up (~36s)…" -f $p.Id)

for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 1
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if ($h.ready -and $h.screen) {
            Write-Output ("ready: provider={0} warmup={1:N1}s" -f $h.active, ($h.warmup_ms / 1000))
            Write-Output ("screen: {0} 块屏，虚拟桌面 {1}x{2}@{3},{4}" -f `
                    $h.screen.monitors.Count, $h.screen.virtual.w, $h.screen.virtual.h, `
                    $h.screen.virtual.x, $h.screen.virtual.y)
            return
        }
    }
    catch { }
}
Write-Output "startup timed out, check $errLog"
