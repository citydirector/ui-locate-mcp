# 一次性环境准备：建 venv、装依赖、下模型。
#
# 为什么需要这一步
#   插件包本身只有代码，模型有 80MB、依赖里有 CUDA 运行库，都不适合塞进
#   一个 npm 包里。所以「安装插件」和「准备运行环境」分成两步：
#   DSH 负责装插件（node_modules 里的 JS/YAML 部分），本脚本负责 Python 侧。
#
# 它会做什么
#   1. 在 <HomeDir>\venv 建一个独立 venv（不污染系统 Python）
#   2. 装本插件（含 cuda 可选依赖）
#   3. 若 <HomeDir>\models 下没有模型，自动下载
#
# HomeDir 怎么定（与 cordis.patch.yml 里的表达式保持一致）：
#   1. -HomeDir 参数
#   2. $env:UI_LOCATE_HOME
#   3. $env:DSH_HOME\ui-locate        ← 便携版 DSH，数据放 data 目录下
#   4. %LOCALAPPDATA%\ui-locate
#   5. $HOME/.local/share/ui-locate
#
# 用法（在插件根目录或任意位置都行）：
#   pwsh -File scripts/bootstrap.ps1
#   pwsh -File scripts/bootstrap.ps1 -SkipModel      # 已有模型，跳过下载
#   pwsh -File scripts/bootstrap.ps1 -CpuOnly        # 不装 CUDA 运行库
#   pwsh -File scripts/bootstrap.ps1 -Editable       # 可编辑安装（改源码即生效）
#   pwsh -File scripts/bootstrap.ps1 -Status         # 只看现状，不动手

param(
    [switch]$SkipModel,
    [switch]$CpuOnly,
    [switch]$Editable,
    [switch]$Status,
    [string]$HomeDir = ''
)

$ErrorActionPreference = 'Stop'

# ── 路径 ────────────────────────────────────────────────────────────────────
$pkgRoot = Split-Path -Parent $PSScriptRoot
if (-not $HomeDir) { $HomeDir = $env:UI_LOCATE_HOME }
if (-not $HomeDir -and $env:DSH_HOME) { $HomeDir = Join-Path $env:DSH_HOME 'ui-locate' }
if (-not $HomeDir) {
    $HomeDir = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'ui-locate' }
            else { Join-Path $HOME '.local/share/ui-locate' }
}
$venv = Join-Path $HomeDir 'venv'
$py = Join-Path $venv 'Scripts/python.exe'
$models = Join-Path $HomeDir 'models'
$modelFile = Join-Path $models 'omniparser_icon_detect_yolo11m.onnx'

# 仓库里只有 onnx/model.onnx 这一个文件
$modelUrl = 'https://huggingface.co/onnx-community/OmniParser-v2.0_icon_detect/resolve/main/onnx/model.onnx'

function Show-Status {
    Write-Output "home   : $HomeDir"
    Write-Output ("venv   : " + $(if (Test-Path $py) { "OK  $py" } else { '缺失' }))
    Write-Output ("model  : " + $(if (Test-Path $modelFile) {
                $mb = [math]::Round((Get-Item $modelFile).Length / 1MB, 1)
                "OK  $modelFile ($mb MB)"
            } else { "缺失  $modelFile" }))
    if (Test-Path $py) {
        try {
            $prov = & $py -c "from ui_locate import config; print(config.resolve_provider())" 2>&1
            Write-Output "provider: $prov"
        }
        catch { Write-Output "provider: 查询失败（插件可能没装进 venv）" }
    }
}

if ($Status) { Show-Status; return }

# ── 1. venv ─────────────────────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $HomeDir, $models | Out-Null

if (-not (Test-Path $py)) {
    Write-Output "创建 venv: $venv"
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        & uv venv --python 3.12 $venv
    }
    else {
        $sysPython = Get-Command python -ErrorAction SilentlyContinue
        if (-not $sysPython) {
            Write-Output '找不到 python 也找不到 uv。请先安装 uv：https://docs.astral.sh/uv/'
            exit 1
        }
        & $sysPython.Source -m venv $venv
    }
}

# ── 2. 装依赖 ───────────────────────────────────────────────────────────────
$extras = if ($CpuOnly) { '' } else { '[cuda]' }
$target = "$pkgRoot$extras"
Write-Output "安装依赖: $target$(if ($Editable) { '  (editable)' })"

# 参数数组而不是拼一个空字符串 —— 空字符串会作为一个真实参数传给 pip，
# 被当成"要安装的包名"而报错。
$installArgs = @('pip', 'install')
if ($Editable) { $installArgs += '--editable' }
$installArgs += $target

$uv2 = Get-Command uv -ErrorAction SilentlyContinue
if ($uv2) {
    & uv @installArgs --python $py
}
else {
    & $py -m pip install --upgrade pip
    & $py -m @installArgs
}

# ── 3. 模型 ─────────────────────────────────────────────────────────────────
if (-not $SkipModel -and -not (Test-Path $modelFile)) {
    Write-Output "下载模型（约 80MB）…"
    Write-Output "  来源: $modelUrl"
    try {
        $tmpModel = "$modelFile.part"
        Invoke-WebRequest -Uri $modelUrl -OutFile $tmpModel -UseBasicParsing
        Move-Item -Path $tmpModel -Destination $modelFile -Force
        $mb = [math]::Round((Get-Item $modelFile).Length / 1MB, 1)
        Write-Output "  完成: $modelFile ($mb MB)"
    }
    catch {
        Write-Output "  下载失败: $_"
        Write-Output "  请手动下载后放到: $modelFile"
        Write-Output "  然后重跑本脚本，或设环境变量 UI_LOCATE_MODEL 指向该文件"
        exit 1
    }
}
elseif (Test-Path $modelFile) {
    Write-Output "模型已存在，跳过下载"
}

Write-Output ''
Show-Status
Write-Output ''
Write-Output '准备完成。在 DSH 里启用 ui-locate 组合包即可使用 mcp__ui-locate__* 工具。'
