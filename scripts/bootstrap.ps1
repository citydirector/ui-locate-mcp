# 一次性环境准备：建 venv、装依赖、下模型。
#
# 为什么需要这一步
#   插件包本身只有代码，模型有 77MB、依赖有一两万个文件，都不适合塞进
#   一个 npm 包里。所以「安装插件」和「准备运行环境」分成两步：
#   DSH 负责装插件（node_modules 里的 JS/YAML 部分），本脚本负责 Python 侧。
#
# 它会做什么
#   1. 在 <HomeDir>\venv 建一个独立 venv（不污染系统 Python）
#   2. 装本插件（默认 DirectML 后端，约 130MB；-Cuda 换成 CUDA，约 1.8GB）
#   3. 若模型目录下没有模型，自动下载
#
# 推理后端怎么选
#   默认 DirectML：走 D3D12，NVIDIA / AMD / Intel 的显卡都能用，实测检测
#   速度与 CUDA 基本持平，依赖体积只有后者的 1/13。
#   传 -Cuda 换成 CUDA：快几个百分点，但只有 NVIDIA 卡能用。
#   传 -CpuOnly 只装纯 CPU 版：没有显卡或只想试一下时用。
#
# 模型目录怎么定（与 src/ui_locate/config.py 的查找顺序一致）：
#   1. -ModelDir 参数
#   2. $env:UI_LOCATE_MODEL_DIR
#   3. <HomeDir>\models                 ← 跟着状态目录走
#
# 想把手里的模型（比如已有的 77MB 文件）直接用上，就别下第二份：设
# $env:UI_LOCATE_MODEL_DIR 指向那个目录，或者传 -ModelDir。脚本按
# 「先找现成的、找不到才下」的顺序走，找到就不再下载。
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
#   pwsh -File scripts/bootstrap.ps1 -Cuda           # 换成 CUDA 后端
#   pwsh -File scripts/bootstrap.ps1 -CpuOnly        # 只装纯 CPU 版
#   pwsh -File scripts/bootstrap.ps1 -Editable       # 可编辑安装（改源码即生效）
#   pwsh -File scripts/bootstrap.ps1 -Status         # 只看现状，不动手

param(
    [switch]$SkipModel,
    [switch]$Cuda,
    [switch]$CpuOnly,
    [switch]$Editable,
    [switch]$Status,
    [string]$HomeDir = '',
    [string]$ModelDir = ''
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

# 模型目录独立于状态目录：它可能很大（77MB 起），也可能早就存在别处。
if (-not $ModelDir) { $ModelDir = $env:UI_LOCATE_MODEL_DIR }
if (-not $ModelDir) { $ModelDir = Join-Path $HomeDir 'models' }
$models = $ModelDir
$modelFile = Join-Path $models 'omniparser_icon_detect_yolo11m.onnx'

# 已经有一份模型在别处（UI_LOCATE_MODEL 指着的那个）就别下第二份。
$existingModel = $env:UI_LOCATE_MODEL

# 仓库里只有 onnx/model.onnx 这一个文件
$modelUrl = 'https://huggingface.co/onnx-community/OmniParser-v2.0_icon_detect/resolve/main/onnx/model.onnx'

function Show-Status {
    Write-Output "home   : $HomeDir"
    Write-Output ("venv   : " + $(if (Test-Path $py) { "OK  $py" } else { '缺失' }))
    Write-Output "models : $models$(if ($models -ne (Join-Path $HomeDir 'models')) { '  (自定义)' })"
    Write-Output ("model  : " + $(if (Test-Path $modelFile) {
                $mb = [math]::Round((Get-Item $modelFile).Length / 1MB, 1)
                "OK  $modelFile ($mb MB)"
            } elseif ($existingModel -and (Test-Path $existingModel)) {
                "复用 $existingModel（UI_LOCATE_MODEL 指向的文件）"
            } else { "缺失  $modelFile" }))
    if (Test-Path $py) {
        try {
            # 一次子进程里问三件事：解析出的 provider、ORT 版本、它到底认哪些后端
            $script = 'from ui_locate import config; import onnxruntime as o; ' +
                      'print(config.resolve_provider()); print(o.__version__); ' +
                      'print(",".join(o.get_available_providers()))'
            $out = & $py -c $script 2>&1
            if ($LASTEXITCODE -eq 0 -and $out.Count -ge 3) {
                Write-Output "provider: $($out[0])  (onnxruntime $($out[1]))"
                Write-Output "backend : $($out[2])"
            }
            else { Write-Output "provider: 查询失败 $out" }
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
# 三种后端的装法不一样，因为 ORT 的三个 wheel 都提供 `onnxruntime` 这个
# 顶层包，装两个就会互相覆盖 —— 后装的赢，而且不报错。所以：
#
#   DirectML  直接装本包，让 pyproject 的默认依赖决定拉哪个 ORT。
#   CUDA/CPU  先 --no-deps 只装代码，再显式装依赖，避免默认的 DirectML
#             那一份一起进来打架（CUDA 版尤其要命：1.8GB 白下）。
if ($Cuda -and $CpuOnly) {
    Write-Output '-Cuda 与 -CpuOnly 不能同时用。'
    exit 1
}

$uv2 = Get-Command uv -ErrorAction SilentlyContinue
$useUv = [bool]$uv2

function Invoke-Pip([string[]]$Arguments) {
    if ($useUv) { & uv pip @Arguments --python $py }
    else { & $py -m pip @Arguments }
}

# 除了 ORT 之外，pyproject 里声明的运行时依赖。改动 pyproject 时这里要同步。
$commonDeps = @(
    'mcp>=1.10,<2', 'numpy>=2.0', 'opencv-python-headless',
    'pillow', 'rapidocr>=3.0', 'httpx'
)

if ($Cuda) {
    Write-Output "安装依赖: $pkgRoot  (CUDA 后端，约 1.8GB)"
    $codeArgs = @('install', '--no-deps')
    if ($Editable) { $codeArgs += '--editable' }
    $codeArgs += $pkgRoot
    Invoke-Pip $codeArgs
    Invoke-Pip (@('install', 'onnxruntime-gpu>=1.23,<1.24', 'nvidia-cudnn-cu12>=9,<10',
            'nvidia-cublas-cu12', 'nvidia-cufft-cu12', 'nvidia-cuda-runtime-cu12') + $commonDeps)
}
elseif ($CpuOnly) {
    Write-Output "安装依赖: $pkgRoot  (纯 CPU)"
    $codeArgs = @('install', '--no-deps')
    if ($Editable) { $codeArgs += '--editable' }
    $codeArgs += $pkgRoot
    Invoke-Pip $codeArgs
    Invoke-Pip (@('install', 'onnxruntime>=1.24,<1.25') + $commonDeps)
}
else {
    Write-Output "安装依赖: $pkgRoot  (DirectML 后端，约 130MB)$(if ($Editable) { '  (editable)' })"
    $codeArgs = @('install')
    if ($Editable) { $codeArgs += '--editable' }
    $codeArgs += $pkgRoot
    Invoke-Pip $codeArgs
}

# ── 3. 模型 ─────────────────────────────────────────────────────────────────
if ($SkipModel) {
    Write-Output '按 -SkipModel 跳过模型检查'
}
elseif (Test-Path $modelFile) {
    Write-Output "模型已存在，跳过下载"
}
elseif ($existingModel -and (Test-Path $existingModel)) {
    Write-Output "已有模型可用，不重复下载：$existingModel"
    Write-Output "（Python 侧按 UI_LOCATE_MODEL 找它，无需再放一份到 $models）"
}
else {
    Write-Output "下载模型（约 77MB）…"
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

Write-Output ''
Show-Status
Write-Output ''
Write-Output '准备完成。在 DSH 里启用 ui-locate 组合包即可使用 mcp__ui-locate__* 工具。'
