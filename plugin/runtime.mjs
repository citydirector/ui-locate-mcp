/**
 * 运行环境的准备：venv、依赖、模型。
 *
 * ## 为什么要自动做这件事
 *
 * 插件管理器只装 JS/YAML 那部分。Python 侧的 venv 与 77MB 的检测模型都在
 * 包外，本该由用户手动跑一次 `scripts/bootstrap.ps1`。但那一步对普通用户
 * 是断掉的：机器上未必有 uv，有 uv 也可能被权限拦住，系统的 `python` 在
 * Windows 上常常只是个跳转到商店的存根。
 *
 * 这里改成由 Host 自己来：DSH 自带了 Python 运行时，路径可以从
 * `process.execPath` 推出来，不需要用户装任何东西。
 *
 * ## 三个必须绕开的坑（都实测过）
 *
 * 1. **`python -m venv` 直接跑会 `WinError 5`**。它默认带 `ensurepip`，而那
 *    一步会在 Scripts 目录里写东西并失败。加 `--without-pip` 就干净了。
 * 2. **pip 于是不存在**，而 `get-pip.py` 也用不了——它要往系统临时目录写
 *    `pip.zip`，在受限环境下 `PermissionError`。绕法：pip 的 wheel 就是个
 *    zip，用 venv 里那个 python 自带的 `zipfile` 模块解到 `site-packages`
 *    即算安装。实测解完 `python -m pip --version` 立刻可用。
 * 3. **`uv` 在 Windows 上多半跑不起来**（缓存锁与解释器探测都被拦），所以
 *    整个流程只用 Python 自己，不依赖任何外部工具。
 *
 * ## 这个模块的可测性
 *
 * 外部世界（跑子进程、下载、读写文件、时钟）全部从参数注入，测试里不需要
 * 真建 venv，也不需要网络。
 */

import { existsSync } from "node:fs"
import { dirname, join } from "node:path"

/** 检测模型在 HuggingFace 上的地址与落盘名。 */
export const MODEL_URL =
  "https://huggingface.co/onnx-community/OmniParser-v2.0_icon_detect/resolve/main/onnx/model.onnx"
export const MODEL_FILENAME = "omniparser_icon_detect_yolo11m.onnx"
/** 与 Python 侧 `config.py` 的 `MODEL_FILENAME` 保持一致；写错就对不上。 */
export const MODEL_BYTES_HINT = 80_305_670

/** pip 的 wheel 地址：按版本钉死，免得哪天上游改了文件名。 */
export const PIP_VERSION = "26.2.2"
export const PIP_WHEEL_URL =
  `https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-${PIP_VERSION}-py3-none-any.whl`

/**
 * 三个推理后端。
 *
 * 三个 wheel 都提供 `onnxruntime` 这个**同一个顶层包**，所以它们互斥，装哪个
 * 就是哪个。这也意味着换后端必须先卸掉旧的，否则新装的会被旧的 residue 挡住。
 */
export const BACKENDS = {
  /** 走 D3D12，NVIDIA / AMD / Intel 通吃，24MB。默认。 */
  directml: "onnxruntime-directml>=1.24,<1.25",
  /** 只有 NVIDIA 能用，比 DirectML 快几个百分点，代价是 1.7GB。 */
  cuda: "onnxruntime-gpu>=1.23,<1.24",
  /** 没有可用显卡时的兜底。 */
  cpu: "onnxruntime>=1.24,<1.25",
}
export const DEFAULT_BACKEND = "directml"

/** 非后端依赖。与 `pyproject.toml` 的 dependencies 去掉 ORT 后必须一致。 */
export const COMMON_DEPS = [
  "mcp>=1.10,<2",
  "numpy>=2.0",
  "opencv-python-headless",
  "pillow",
  "rapidocr>=3.0",
  "httpx",
]

/** CUDA 后端额外要的 nvidia 运行库（与 `pyproject.toml` 的 [cuda] 对齐）。 */
export const CUDA_RUNTIME_DEPS = [
  "nvidia-cublas-cu12",
  "nvidia-cudnn-cu12",
  "nvidia-cufft-cu12",
  "nvidia-cuda-nvrtc-cu12",
]

/** 某个后端要装的全部包。 */
export function backendPackages(backend, platform = process.platform) {
  const chosen = BACKENDS[backend] ? backend : DEFAULT_BACKEND
  // 非 Windows 没有 DirectML，无论要哪个都退回 CPU 版；要 CUDA 得显式点名。
  const effective = platform !== "win32" && chosen === "directml" ? "cpu" : chosen
  const packages = [...COMMON_DEPS, BACKENDS[effective]]
  if (effective === "cuda") packages.push(...CUDA_RUNTIME_DEPS)
  return { backend: effective, packages }
}

/**
 * 从宿主可执行文件推出 DSH 自带的 Python。
 *
 * Electron 的 `process.execPath` 是 `...\DeepSeek Harness.exe`，运行时就在它
 * 旁边的 `resources\runtime\primary-runtime\dependencies\`。用相对推导而不是
 * 写死盘符，是为了便携版整体搬走后还能找到。
 *
 * @param execPath - 宿主可执行文件路径。
 * @param platform - 目标平台，决定 python 可执行文件名。
 * @returns 候选路径列表（先到先用）。
 */
export function bundledPythonCandidates(execPath, platform = process.platform) {
  const exeName = platform === "win32" ? "python.exe" : "python3"
  const root = dirname(execPath)
  const deps = join(root, "resources", "runtime", "primary-runtime", "dependencies", "python")
  return [
    join(deps, exeName),
    // 开发版（未打包）时运行时还在别处，兜到仓库上一层的同名目录。
    join(root, "..", "resources", "runtime", "primary-runtime", "dependencies", "python", exeName),
  ]
}

/**
 * venv 里的解释器路径。
 *
 * @param home - 运行状态目录。
 * @param platform - 目标平台。
 */
export function venvPython(home, platform = process.platform) {
  return platform === "win32"
    ? join(home, "venv", "Scripts", "python.exe")
    : join(home, "venv", "bin", "python")
}

/**
 * venv 里 `site-packages` 的位置。
 *
 * Windows 是 `Lib\site-packages`，POSIX 是 `lib/python3.x/site-packages`，
 * 版本号得从解释器问出来，所以这个函数要拿到版本。
 *
 * @param home - 运行状态目录。
 * @param version - 形如 `3.12.14` 或 `3.12`。
 * @param platform - 目标平台。
 */
export function sitePackages(home, version, platform = process.platform) {
  if (platform === "win32") return join(home, "venv", "Lib", "site-packages")
  const short = String(version ?? "").split(".").slice(0, 2).join(".") || "3"
  return join(home, "venv", "lib", `python${short}`, "site-packages")
}

/**
 * 把一条原始输入解释成绝对路径。
 *
 * 用户在设置页里可能填 `~`，也可能留空表示「用默认」。留空时退回默认值，
 * 而不是拼出一个 `<cwd>/undefined`。
 *
 * @param raw - 用户输入。
 * @param fallback - 留空时的默认值。
 * @param home - 用来展开 `~` 的家目录。
 */
export function resolvePath(raw, fallback, home) {
  const text = typeof raw === "string" ? raw.trim() : ""
  if (text === "") return fallback
  if (text === "~") return home
  if (text.startsWith("~/") || text.startsWith("~\\")) return join(home, text.slice(2))
  return text
}

/**
 * 这次要往哪里装。
 *
 * 两处位置分开：代码与 venv 跟着状态目录，模型可能很大、也可能是早就存在的
 * 一份，所以允许单独指定。
 *
 * @param options - `{ home, modelDir, defaultHome, defaultModelDir, homeDir }`
 * @returns `{ home, modelDir, venv, python, model }`
 */
export function runtimePlan({ home, modelDir, defaultHome, defaultModelDir, homeDir = "" }) {
  const resolvedHome = resolvePath(home, defaultHome, homeDir)
  const resolvedModels = resolvePath(modelDir, defaultModelDir, homeDir)
  return {
    home: resolvedHome,
    modelDir: resolvedModels,
    venv: join(resolvedHome, "venv"),
    python: venvPython(resolvedHome),
    model: join(resolvedModels, MODEL_FILENAME),
  }
}

/**
 * 一次 `execFile`，把退出码与输出收成普通对象。
 *
 * 抛异常和「退出码非零」在调用方看来应该是同一件事，统一成 `ok: false`。
 */
function run(exec, command, args, options = {}) {
  return new Promise((resolve) => {
    exec(command, args, { timeout: 600_000, maxBuffer: 8 << 20, ...options }, (error, stdout, stderr) => {
      resolve({
        ok: error === null || error === undefined,
        code: typeof error?.code === "number" ? error.code : error ? -1 : 0,
        stdout: String(stdout ?? ""),
        stderr: String(stderr ?? ""),
        error,
      })
    })
  })
}

/** 把子进程输出压成一行，够放进日志与报错就够了。 */
function brief(result, limit = 400) {
  const text = (result.stderr || result.stdout || "").trim().replace(/\s+/g, " ")
  if (text === "") return result.error ? String(result.error.message ?? result.error) : `exit ${result.code}`
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}

/**
 * 下载一个文件，先写 `.part` 再改名。
 *
 * 中途失败时留下的是 `.part` 而不是半截的正式文件——后者会被下一次启动
 * 当成「模型已就绪」，然后在推理时报一个看不懂的错。
 *
 * @returns `{ ok, bytes }` 或 `{ ok: false, error }`
 */
export async function download(url, destination, { fetch = globalThis.fetch, signal, onProgress } = {}) {
  let response
  try {
    response = await fetch(url, { signal, redirect: "follow" })
  } catch (error) {
    return { ok: false, error: `下载失败：${String(error?.message ?? error)}` }
  }
  if (!response?.ok) return { ok: false, error: `下载失败：HTTP ${response?.status ?? "?"}` }
  const total = Number(response.headers?.get?.("content-length") ?? 0)
  const part = `${destination}.part`
  let written = 0
  try {
    const { createWriteStream } = await import("node:fs")
    const { pipeline } = await import("node:stream/promises")
    const { Readable } = await import("node:stream")
    const file = createWriteStream(part)
    const source = Readable.fromWeb(response.body)
    if (onProgress) {
      source.on("data", (chunk) => {
        written += chunk.length
        onProgress(written, total)
      })
    }
    await pipeline(source, file)
    const { rename } = await import("node:fs/promises")
    await rename(part, destination)
    return { ok: true, bytes: written }
  } catch (error) {
    try {
      const { rm } = await import("node:fs/promises")
      await rm(part, { force: true })
    } catch {
      /* 清理失败不该盖住真正的原因 */
    }
    return { ok: false, error: `下载中断：${String(error?.message ?? error)}` }
  }
}

/**
 * 把 pip 装进一个没有 pip 的 venv。
 *
 * 不用 `get-pip.py`（它要往系统临时目录写东西，受限环境下必失败），而是把
 * pip 的 wheel 当作普通 zip 解到 `site-packages`。wheel 就是 zip，解完就是
 * 安装好的样子，连 `RECORD` 都对得上。
 *
 * @returns `{ ok, error? }`
 */
export async function bootstrapPip(python, home, version, {
  exec,
  fetch = globalThis.fetch,
  platform = process.platform,
  tmpDir,
  wheelUrl = PIP_WHEEL_URL,
  signal,
} = {}) {
  const wheel = join(tmpDir ?? home, `pip-${PIP_VERSION}.whl`)
  const got = await download(wheelUrl, wheel, { fetch, signal })
  if (!got.ok) return { ok: false, error: `取 pip 失败：${got.error}` }
  const target = sitePackages(home, version, platform)
  const unzipped = await run(exec, python, ["-m", "zipfile", "-e", wheel, target])
  if (!unzipped.ok) return { ok: false, error: `解 pip 失败：${brief(unzipped)}` }
  const check = await run(exec, python, ["-m", "pip", "--version"])
  if (!check.ok) return { ok: false, error: `pip 装上了但用不了：${brief(check)}` }
  return { ok: true, pip: check.stdout.trim().split("\n")[0] }
}

/** 读 venv 里 Python 的版本号；拿不到就返回 undefined，不抛。 */
async function pythonVersion(exec, python) {
  const result = await run(exec, python, ["-c", "import sys;print(sys.version.split()[0])"])
  if (!result.ok) return undefined
  const first = result.stdout.trim().split("\n")[0]
  return /^\d+\.\d+/.test(first) ? first : undefined
}

/**
 * 准备运行环境。可重复调用：已经是好的就什么都不做。
 *
 * 步骤按依赖顺序：venv → pip → 本包 → 模型。任何一步失败就停下并带着
 * 已完成的步骤一起返回，让设置页能显示「卡在哪」而不是只说失败。
 *
 * @param options
 *   - `home` / `modelDir`：目标位置（已解析过的绝对路径）。
 *   - `pythonExe`：用来创建 venv 的解释器（DSH 自带的那个）。
 *   - `packageRoot`：本插件的根目录，作为 pip 的安装目标。
 *   - `force`：为真时删掉 venv 与模型重来。
 *   - `exec` / `fetch` / `exists`：注入点，测试里替换掉。
 * @returns `{ ok, steps, error? }`
 */
export async function ensureRuntime({
  home,
  modelDir,
  pythonExe,
  packageRoot,
  backend: requestedBackend = DEFAULT_BACKEND,
  force = false,
  exec,
  fetch = globalThis.fetch,
  exists = existsSync,
  platform = process.platform,
  signal,
  log = () => {},
  onProgress,
}) {
  const steps = []
  const done = (name, detail) => {
    steps.push({ name, ...detail })
    log(`${name}: ${JSON.stringify(detail)}`)
  }
  const fail = (name, error) => {
    steps.push({ name, ok: false, error })
    return { ok: false, steps, error }
  }

  const plan = runtimePlan({ home, modelDir, defaultHome: home, defaultModelDir: modelDir })
  const python = plan.python
  const venvDir = plan.venv

  const { mkdir, rm } = await import("node:fs/promises")

  try {
    await mkdir(plan.home, { recursive: true })
    await mkdir(plan.modelDir, { recursive: true })
  } catch (error) {
    return fail("目录", `建不了目录：${String(error?.message ?? error)}`)
  }

  // ── 1. venv ────────────────────────────────────────────────────────────────
  let reusable = exists(python)
  if (reusable && force) {
    try {
      await rm(venvDir, { recursive: true, force: true })
    } catch (error) {
      return fail("venv", `删不掉旧 venv：${String(error?.message ?? error)}`)
    }
    reusable = false
  }
  if (reusable) {
    done("venv", { ok: true, path: venvDir, reused: true })
  } else {
    // `--without-pip` 是关键：带上 ensurepip 就会在受限环境下 WinError 5。
    const created = await run(exec, pythonExe, ["-m", "venv", "--without-pip", venvDir])
    if (!created.ok || !exists(python)) {
      return fail("venv", `建 venv 失败：${brief(created)}`)
    }
    done("venv", { ok: true, path: venvDir, reused: false })
  }

  const version = await pythonVersion(exec, python)
  if (version === undefined) return fail("venv", "venv 建好了但解释器跑不起来")

  // ── 2. pip ─────────────────────────────────────────────────────────────────
  const hasPip = await run(exec, python, ["-m", "pip", "--version"])
  if (hasPip.ok) {
    done("pip", { ok: true, reused: true, version: hasPip.stdout.trim().split("\n")[0] })
  } else {
    const bootstrapped = await bootstrapPip(python, plan.home, version, {
      exec,
      fetch,
      platform,
      tmpDir: plan.home,
      signal,
    })
    if (!bootstrapped.ok) return fail("pip", bootstrapped.error)
    done("pip", { ok: true, reused: false, version: bootstrapped.pip })
  }

  // ── 3. 依赖 ────────────────────────────────────────────────────────────────
  //
  // 不把依赖交给 `pip install <本包>` 自己去解，原因有两个：
  //
  //   a) 后端必须能选。三个 ORT wheel（directml / gpu / 纯 CPU）都提供同一个
  //      `onnxruntime` 顶层包，谁后装谁覆盖；让 pip 按 pyproject 的 marker
  //      去解，就永远只能得到默认那个。
  //   b) `--no-deps` 装本包时才不会顺带拉一遍。
  //
  // 所以依赖在这里显式列出，并按用户选的后端决定装哪个 ORT。
  const { backend, packages: deps } = backendPackages(requestedBackend, platform)
  const env = { ...process.env, TEMP: plan.home, TMP: plan.home, TMPDIR: plan.home }
  const depsArgs = ["-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check"]
  if (force) depsArgs.push("--force-reinstall", "--upgrade")
  depsArgs.push(...deps)
  const depsInstalled = await run(exec, python, depsArgs, { env })
  if (!depsInstalled.ok) return fail("依赖", `装依赖失败：${brief(depsInstalled)}`)
  done("依赖", { ok: true, backend, packages: deps.length })

  // ── 4. 本包 ────────────────────────────────────────────────────────────────
  // `--no-deps` 是有意的：上面已经把依赖按选定的后装好了，这里再让 pip 解一遍
  // 只会把后端换回 pyproject 里的默认值。
  const installArgs = ["-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "--no-deps"]
  if (force) installArgs.push("--force-reinstall", "--upgrade")
  installArgs.push(packageRoot)
  const installed = await run(exec, python, installArgs, { env })
  if (!installed.ok) return fail("本包", `装 Python 侧失败：${brief(installed)}`)
  done("本包", { ok: true, path: packageRoot })

  // ── 4. 模型 ────────────────────────────────────────────────────────────────
  if (exists(plan.model) && !force) {
    done("模型", { ok: true, path: plan.model, reused: true })
  } else {
    const got = await download(MODEL_URL, plan.model, { fetch, signal, onProgress })
    if (!got.ok) return fail("模型", got.error)
    done("模型", { ok: true, path: plan.model, bytes: got.bytes })
  }

  return { ok: true, steps, plan }
}

/**
 * 只读地看一眼现状，不动任何东西。设置页打开时先调这个。
 *
 * @returns 位置、存在性、以及 venv 里那个包有没有装上。
 */
export async function inspectRuntime({
  home,
  modelDir,
  packageName,
  exec,
  exists = existsSync,
  statSize,
}) {
  const plan = runtimePlan({ home, modelDir, defaultHome: home, defaultModelDir: modelDir })
  const venv = exists(plan.python)
  const model = exists(plan.model)
  const facts = {
    home: plan.home,
    modelDir: plan.modelDir,
    venv,
    model,
    modelBytes: model ? statSize?.(plan.model) : undefined,
    pythonVersion: undefined,
    installedVersion: undefined,
    backend: undefined,
  }
  if (!venv || !exec) return facts
  const probe = [
    "import json,sys",
    `try:`,
    `    import importlib.metadata as m; v=m.version(${JSON.stringify(packageName)})`,
    `except Exception: v=None`,
    `try:`,
    `    import onnxruntime as o; b=",".join(o.get_available_providers())`,
    `except Exception: b=None`,
    `print(json.dumps({"python":sys.version.split()[0],"pkg":v,"backend":b}))`,
  ].join("\n")
  const result = await run(exec, plan.python, ["-c", probe])
  if (result.ok) {
    try {
      const parsed = JSON.parse(result.stdout.trim().split("\n").pop())
      facts.pythonVersion = parsed.python ?? undefined
      facts.installedVersion = parsed.pkg ?? undefined
      facts.backend = parsed.backend ?? undefined
    } catch {
      /* 输出不是 JSON 就只报「venv 在」这一层 */
    }
  }
  return facts
}
