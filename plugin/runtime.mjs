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

import { createHash } from "node:crypto"
import { createWriteStream, existsSync, readFileSync, statSync } from "node:fs"
import { dirname, join } from "node:path"

import { NO_DEPS_WIN_DEPS } from "./core.mjs"

/**
 * 检测模型的源，按顺序试。
 *
 * 第二个是 HuggingFace 的国内镜像：**路径结构完全相同，只换域名**，所以在
 * 代码里只是同一串路径前缀的替换。顺序是先官方后镜像——镜像站是公益项目，
 * 能少给它一点压力就少一点，而且对国外用户来说官方那条本来更快。
 */
const MODEL_PATH = "onnx-community/OmniParser-v2.0_icon_detect/resolve/main/onnx/model.onnx"
export const MODEL_SOURCES = [
  `https://huggingface.co/${MODEL_PATH}`,
  `https://hf-mirror.com/${MODEL_PATH}`,
]
export const MODEL_URL = MODEL_SOURCES[0]
export const MODEL_FILENAME = "omniparser_icon_detect_yolo11m.onnx"
/** 与 Python 侧 `config.py` 的 `MODEL_FILENAME` 保持一致；写错就对不上。 */
export const MODEL_BYTES_HINT = 80_305_670
/**
 * 模型文件的 sha256，取自 HF 仓库里 `onnx/model.onnx` 的 LFS oid。
 *
 * 下完必须对上：一个截断的模型要到真正推理时才炸，而那时用户已经在用工具了，
 * 报出来的还是 onnxruntime 内部的一句话。宁可在下载这一步就说「校验不符」。
 */
export const MODEL_SHA256 = "c0d3d261e3368a5e51fe0d632c5d62e4bd4da489ac4b95fbbee450eb640e36fe"

/**
 * pip 的 wheel，同样给多个源。
 *
 * 三个地址只差前缀：`files.pythonhosted.org` 是官方，两个镜像站原样搬了
 * `packages/<hash 路径>` 这一层，所以文件名与目录结构可以共用一份常量。
 * 版本钉死，免得哪天上游改了文件名。
 */
const PIP_WHEEL_PATH =
  `packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl`
export const PIP_VERSION = "26.2.2"
export const PIP_SOURCES = [
  `https://files.pythonhosted.org/${PIP_WHEEL_PATH}`,
  `https://pypi.tuna.tsinghua.edu.cn/${PIP_WHEEL_PATH}`,
  `https://mirrors.aliyun.com/pypi/${PIP_WHEEL_PATH}`,
]
export const PIP_WHEEL_URL = PIP_SOURCES[0]
/** pip 26.2.2 wheel 的 sha256（PyPI JSON API 的 `digests.sha256`）。 */
export const PIP_SHA256 = "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"

/**
 * 装依赖时用的索引，第一个是官方，后面是国内镜像。
 *
 * 与 wheel 不同，这里**不预先探测**：探测本身也要一次往返，而正常网络下官方
 * 索引直接就通了。改成「先按默认装，失败再换镜像重来一次」——坏网络才会多花
 * 那一轮，好网络一分钱不多付。
 */
export const PIP_INDEXES = [
  "https://pypi.org/simple",
  "https://pypi.tuna.tsinghua.edu.cn/simple",
  "https://mirrors.aliyun.com/pypi/simple",
]

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

/**
 * 缺了就跑不起来的依赖。
 *
 * `mcp_server.py` 在模块顶层 `import httpx`，`server.py` 顶层 `import cv2`，
 * `detect.py` 顶层 `import onnxruntime`——这几个少一个，服务进程根本起不来。
 */
export const ESSENTIAL_DEPS = [
  "mcp>=1.10,<2",
  "httpx",
  "numpy>=2.0",
  "opencv-python-headless",
]

/**
 * 缺了只降级的依赖。全部是**函数内部**才 import 的，所以装不上时服务照样起。
 *
 * * `rapidocr`：`locate.py:86` 里现 import，缺了等于没有 OCR，只剩图标检测。
 * * `dxcam`：`capture.py:164` 现 import，缺了屏幕抓取退到 WGC / GDI。
 * * `pillow`：`capture.py:130` 的 `ImageGrab`——最后一级的 GDI 兜底，缺了只剩
 *   DXGI 与 WGC 两条路。
 *
 * 放在可选这一步，是因为它们在坏网络下恰恰是最容易失败的那几个（rapidocr 会
 * 拖一串 onnxruntime 相关的依赖）。让它们挡住整个安装，用户就一个功能都用不上。
 */
export const OPTIONAL_DEPS = [
  "rapidocr>=3.0",
  "dxcam>=0.3,<0.4; sys_platform == 'win32'",
  "pillow",
]

/** 两档合起来，与 `pyproject.toml` 的 dependencies 去掉 ORT 后必须一致。 */
export const COMMON_DEPS = [...ESSENTIAL_DEPS, ...OPTIONAL_DEPS]


// 只在 Windows、用 --no-deps 装的包：定义在 core.mjs，两条安装路径共用一份。
export { NO_DEPS_WIN_DEPS }

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
  const essential = [...ESSENTIAL_DEPS, BACKENDS[effective]]
  if (effective === "cuda") essential.push(...CUDA_RUNTIME_DEPS)
  const optional = [...OPTIONAL_DEPS]
  // `packages` 是两档的并集，保留给「一次装完」的调用方（含旧测试）。
  return { backend: effective, essential, optional, packages: [...essential, ...optional] }
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
    // 抛异常和「退出码非零」在调用方看来应该是同一件事，统一成 ok:false。
    //
    // **exec 必须在 try 里调**：execFile 有两种失败姿势 —— 进回调（进程起来
    // 了、退出码非零），以及根本没起来时**同步 throw**（spawn EPERM / ENOENT）。
    // 后者在 new Promise(executor) 里抛会直接冒出构造器，调用方拿到的是同步
    // 异常而不是 rejected Promise。这个差别在「没人接 rejection」的场合很致命，
    // 见 core.mjs 的 runProcess 与 README 坑 33。
    const settle = (error, stdout, stderr) =>
      resolve({
        ok: error === null || error === undefined,
        code: typeof error?.code === "number" ? error.code : error ? -1 : 0,
        stdout: String(stdout ?? ""),
        stderr: String(stderr ?? ""),
        error,
      })
    try {
      exec(command, args, { timeout: 600_000, maxBuffer: 8 << 20, ...options }, (error, stdout, stderr) => {
        settle(error, stdout, stderr)
      })
    } catch (error) {
      // spawn 层面就失败了：ENOENT / EPERM / EACCES 都走这里
      settle(error, "", String(error?.message ?? error))
    }
  })
}

/** 把子进程输出压成一行，够放进日志与报错就够了。 */
function brief(result, limit = 400) {
  const text = (result.stderr || result.stdout || "").trim().replace(/\s+/g, " ")
  if (text === "") return result.error ? String(result.error.message ?? result.error) : `exit ${result.code}`
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}

/**
 * RID → 名字。与 Python 侧 `window.INTEGRITY_NAMES` 同一套阈值。
 *
 * 降序排列，用 `value >= floor` 逐个比：完整性 RID 是「下限」，比如 0x2100
 * （medium plus）与 0x2000（medium）只差一档，必须从高往低找才不会认错。
 */
const INTEGRITY_NAMES = [
  [0x4000, "system"],
  [0x3000, "high"],
  [0x2100, "medium+"],
  [0x2000, "medium"],
  [0x1000, "low"],
  [0x0000, "untrusted"],
]

/**
 * 进程完整性级别 → 给设置页看的结论。
 *
 * 进程的完整性是「父进程令牌与主映像标签里更低的那个」，所以两种完全不同的
 * 情况都会落到 Low：以低完整性启动 DSH，或者 `python.exe` 本身带了 Low 标签
 * （运行目录被当成工作区时会写这种标签，且可继承）。两者后果一样——WGC 抓
 * 不到画面、输入被 UIPI 拦——而**都不报错**。
 *
 * 这里只给一句人话让设置页有东西可标红；详细解释在服务侧的
 * `window.integrity_warnings()`，那边能说清具体哪些子系统受影响。
 */
export function integrityFacts(rid, uiAccess) {
  const value = Number(rid)
  if (rid === null || rid === undefined || !Number.isFinite(value)) {
    return { level: undefined, ui_access: Boolean(uiAccess), warnings: [] }
  }
  const level = (INTEGRITY_NAMES.find(([floor]) => value >= floor) ?? [0, "untrusted"])[1]
  const warnings = []
  if (level === "low" || level === "untrusted") {
    warnings.push(
      `运行环境在 ${level} 完整性级别：窗口捕获（WGC）会被系统拒绝，被遮挡的窗口会抓成全黑；` +
        "向更高完整性的窗口发送输入会被静默丢弃。多半是运行目录被当成了工作区" +
        "（那里会写入持久、可继承的 Low 标签）——把它挪到工作区之外，或以管理员身份启动 DSH。",
    )
  }
  return { level, ui_access: Boolean(uiAccess), warnings }
}

/** 取文件尺寸；拿不到就返回 undefined —— 这是「不知道」，不是 0。 */
function sizeOf(statSize, path) {
  if (typeof statSize !== "function") return undefined
  try {
    const size = statSize(path)
    return Number.isFinite(size) ? size : undefined
  } catch {
    return undefined
  }
}

/** 报错里只留域名：完整 URL 太长，一屏日志里反而看不出是哪个源挂了。 */
function sourceLabel(url) {
  try {
    return new URL(url).host
  } catch {
    return String(url)
  }
}

/** 这几个状态码换多少次都一样，别在一个 404 上耗掉整轮重试。 */
function isFatalStatus(status) {
  return status === 400 || status === 401 || status === 403 || status === 404 || status === 410
}

const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** 文件内容的 sha256（小写十六进制）。 */
async function fileSha256(path) {
  const { createReadStream } = await import("node:fs")
  const hash = createHash("sha256")
  for await (const chunk of createReadStream(path)) hash.update(chunk)
  return hash.digest("hex")
}

/**
 * 从一个地址往 `.part` 里写，能续就续。
 *
 * 有残留的 `.part` 就带 `Range:`；服务器回 206 才追加，回 200 说明它不认
 * Range，那就从头覆盖写——追加写会拼出一个坏文件，而且尺寸往往看着还挺对。
 *
 * @returns `{ ok, bytes }` / `{ ok: false, error, fatal?, restart? }`
 */
async function fetchInto(url, part, { fetch, signal, onProgress, expectedBytes, measure }) {
  let offset = 0
  const have = measure(part)
  if (have > 0 && (expectedBytes === undefined || have < expectedBytes)) offset = have

  let response
  try {
    response = await fetch(url, {
      signal,
      redirect: "follow",
      headers: offset > 0 ? { range: `bytes=${offset}-` } : {},
    })
  } catch (error) {
    return { ok: false, error: String(error?.message ?? error) }
  }
  if (response?.status === 416) {
    // 服务器说「这个范围不存在」：残留的 `.part` 跟它那一份对不上，删掉重来。
    return { ok: false, error: "HTTP 416（断点位置无效）", restart: true }
  }
  if (!response?.ok) {
    return {
      ok: false,
      error: `HTTP ${response?.status ?? "?"}`,
      fatal: isFatalStatus(response?.status),
    }
  }

  const resuming = offset > 0 && response.status === 206
  if (!resuming) offset = 0
  const length = Number(response.headers?.get?.("content-length") ?? 0)
  const total = length > 0 ? length + offset : 0

  const { pipeline } = await import("node:stream/promises")
  const { Readable, Transform } = await import("node:stream")
  const file = createWriteStream(part, resuming ? { flags: "a" } : { flags: "w" })
  // 计数用 Transform，而不是在源上挂 `data` 监听：挂 `data` 会把流切到
  // flowing 模式，接下来 `pipeline` 再接就已经晚了一步（小文件看不出差别，
  // 80MB 上会丢首块）。
  let written = offset
  const counter = new Transform({
    transform(chunk, _encoding, done) {
      written += chunk.length
      onProgress?.(written, total)
      done(null, chunk)
    },
  })
  await pipeline(Readable.fromWeb(response.body), counter, file)
  return { ok: true, bytes: written }
}


/**
 * 从多个地址里挑一个能用的把文件下下来，先写 `.part` 再改名。
 *
 * ## 为什么要多地址
 *
 * 只有一个地址时，那条链路挂了就等于装不上。模型在 huggingface.co 上，官方
 * 镜像（hf-mirror.com）路径结构完全一样；pip 的 wheel 也一样，三个源只差域名
 * 前缀。所以这里收一个**数组**，按顺序试，第一个成功的就用。
 *
 * ## 为什么要 `.part` 和 Range
 *
 * 77MB 的模型在坏网络上断在一半是常事。旧的写法是失败就把 `.part` 删掉、下次
 * 从头再来——用户看到的是一遍又一遍的「下载中断」。现在失败时**保留** `.part`，
 * 下一轮带 `Range:` 从断点续；服务器不认 Range（回 200）就老实从头覆盖写。
 *
 * ## 为什么要 sha256
 *
 * 半个文件比没有文件更坏：它会被当成「已就绪」，然后在推理时报一句看不懂的
 * 错。给了 `sha256` 就先校验再改名，对不上就删掉换下一个源——宁可多下一遍，
 * 也不留一个看起来装好了的坏文件。
 *
 * @param urls - 一个地址或多个地址（按顺序试）。
 * @param destination - 最终文件路径。
 * @param options
 *   - `fetch` / `signal` / `onProgress`：注入点，测试替换。
 *   - `sha256`：给了就校验，不符则换源重下。
 *   - `expectedBytes`：已知大小，用来判断残留的 `.part` 是否已经完整。
 *   - `attempts`：**每个源**试几次（默认 2）。
 *   - `measure` / `sleep` / `log`：注入点。
 * @returns `{ ok, bytes, url?, reused? }` 或 `{ ok: false, error }`
 */
export async function download(
  urls,
  destination,
  {
    fetch = globalThis.fetch,
    signal,
    onProgress,
    sha256,
    expectedBytes,
    attempts = 2,
    measure = (path) => {
      try {
        return statSync(path).size
      } catch {
        return 0
      }
    },
    sleep = defaultSleep,
    log = () => {},
  } = {},
) {
  const sources = (Array.isArray(urls) ? urls : [urls]).filter(
    (item) => typeof item === "string" && item !== "",
  )
  if (sources.length === 0) return { ok: false, error: "下载失败：没有可用的地址" }
  const part = `${destination}.part`
  const { rename, rm } = await import("node:fs/promises")

  // 上一轮已经下完、只是没来得及改名（或者改名那一步被打断）：先验一下再收工。
  // 白下 80MB 是用户能感觉到的浪费，而验一次 sha256 只要一秒。
  if (sha256 !== undefined && expectedBytes !== undefined && measure(part) === expectedBytes) {
    const digest = await fileSha256(part).catch(() => undefined)
    if (digest === sha256) {
      await rename(part, destination)
      return { ok: true, bytes: expectedBytes, reused: true }
    }
    await rm(part, { force: true }).catch(() => {})
  }

  const failures = []
  const rounds = Math.max(1, attempts)
  for (const url of sources) {
    for (let attempt = 1; attempt <= rounds; attempt += 1) {
      if (signal?.aborted) return { ok: false, error: "下载已取消" }
      let step
      try {
        step = await fetchInto(url, part, { fetch, signal, onProgress, expectedBytes, measure })
      } catch (error) {
        // pipeline 中途断了：`.part` 留着，下一轮从断点接着下。
        step = { ok: false, error: String(error?.message ?? error) }
      }
      if (step.restart === true) {
        await rm(part, { force: true }).catch(() => {})
        log(`下载 ${sourceLabel(url)}: 断点位置无效，从头重来`)
        continue
      }
      if (step.ok !== true) {
        failures.push(`${sourceLabel(url)}: ${step.error}`)
        if (step.fatal === true) break
        if (attempt < rounds) await sleep(attempt * 500)
        continue
      }
      if (sha256 !== undefined) {
        const digest = await fileSha256(part).catch(() => undefined)
        if (digest !== sha256) {
          // 内容坏了，续传只会继续坏下去：删掉，换下一个源重新下。
          await rm(part, { force: true }).catch(() => {})
          const got = String(digest ?? "读不出").slice(0, 12)
          failures.push(`${sourceLabel(url)}: 校验不符（要 ${sha256.slice(0, 12)}… 拿到 ${got}…）`)
          break
        }
      }
      try {
        await rename(part, destination)
      } catch (error) {
        return { ok: false, error: `下载完成但改名失败：${String(error?.message ?? error)}` }
      }
      return { ok: true, bytes: step.bytes, url }
    }
  }
  return { ok: false, error: `下载失败（已试 ${sources.length} 个源）：${failures.join("；")}` }
}

/**
 * 依赖装失败之后，按顺序找一个能用的索引。
 *
 * 只在**失败之后**调用，不预先探测：正常网络下官方索引一次就通，探测纯属多花
 * 一次往返。每个候选先要一次 `simple/pip/`（几十 KB 的列表页），通不过就换下
 * 一个。全都不通时返回 undefined，调用方仍可退回 pip 的默认行为。
 *
 * @returns 可用的索引地址，或 undefined。
 */
export async function pickIndex({
  fetch = globalThis.fetch,
  indexes = PIP_INDEXES,
  probeMs = 5000,
  signal,
  log = () => {},
} = {}) {
  for (const index of indexes) {
    if (signal?.aborted) return undefined
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), probeMs)
    try {
      const response = await fetch(`${index}/pip/`, { signal: controller.signal, redirect: "follow" })
      if (response?.ok) {
        log(`pip index: ${index}`)
        return index
      }
      log(`pip index ${index} → HTTP ${response?.status ?? "?"}`)
    } catch (error) {
      log(`pip index ${index} → ${String(error?.message ?? error)}`)
    } finally {
      clearTimeout(timer)
    }
  }
  return undefined
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
  wheelUrls = PIP_SOURCES,
  signal,
  log = () => {},
} = {}) {
  const wheel = join(tmpDir ?? home, `pip-${PIP_VERSION}.whl`)
  // 三个源只差域名，sha256 是同一个文件——所以校验值只有一份，多源不会
  // 因为「哪个源的内容不一样」而误判。
  const got = await download(wheelUrls, wheel, {
    fetch,
    signal,
    sha256: PIP_SHA256,
    expectedBytes: 1_819_850,
    log,
  })
  if (!got.ok) return { ok: false, error: `取 pip 失败：${got.error}` }
  const target = sitePackages(home, version, platform)
  const unzipped = await run(exec, python, ["-m", "zipfile", "-e", wheel, target])
  // wheel 解完就没用了，留着白占 1.8MB —— 运行目录是给用户的，不该堆垃圾。
  // 删不掉不算失败：pip 已经进去了，这个文件顶多占点地方。
  try {
    const { rm } = await import("node:fs/promises")
    await rm(wheel, { force: true })
  } catch {
    /* 留着就留着 */
  }
  if (!unzipped.ok) return { ok: false, error: `解 pip 失败：${brief(unzipped)}` }
  const check = await run(exec, python, ["-m", "pip", "--version"])
  if (!check.ok) return { ok: false, error: `pip 装上了但用不了：${brief(check)}` }
  return { ok: true, pip: check.stdout.trim().split("\n")[0] }
}

/**
 * 扫掉运行目录里遗留的 pip wheel。
 *
 * 早期版本的 `bootstrapPip` 装完 pip 不删这个文件，于是 1.8 MB 的
 * `pip-26.2.2.whl` 就永久躺在用户的状态目录里。已经装过环境的人不会重走
 * `bootstrapPip`，所以清理要单独挂在「pip 已可用」这条分支上。
 *
 * 只认 `pip-<数字>.whl` 这个形状，别的文件一概不碰——这是用户目录。
 */
async function dropStaleWheels(home) {
  try {
    const { readdir, rm } = await import("node:fs/promises")
    for (const name of await readdir(home)) {
      if (/^pip-\d+(\.\d+)*\.whl$/.test(name)) await rm(join(home, name), { force: true })
    }
  } catch {
    /* 删不掉就算了，不影响功能 */
  }
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
  statSize = (path) => {
    try {
      return statSync(path).size
    } catch {
      return undefined
    }
  },
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
    // 老版本装 pip 时会把 wheel 留在运行目录里（那时还没有下面那句清理）。
    // 走到这里说明 pip 可用，那个文件纯属垃圾，顺手扫掉。
    await dropStaleWheels(plan.home)
    done("pip", { ok: true, reused: true, version: hasPip.stdout.trim().split("\n")[0] })
  } else {
    const bootstrapped = await bootstrapPip(python, plan.home, version, {
      exec,
      fetch,
      platform,
      tmpDir: plan.home,
      signal,
      log,
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
  const { backend, essential, optional } = backendPackages(requestedBackend, platform)
  const env = { ...process.env, TEMP: plan.home, TMP: plan.home, TMPDIR: plan.home }
  // `--timeout 30 --retries 2` 是有意的：pip 默认单次读 15 秒、重试 5 次，
  // 一条死的连接要耗掉 90 秒才认输，后面换镜像那一步就被拖得没意义了。
  const pipInstall = [
    "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check",
    "--timeout", "30", "--retries", "2",
  ]
  if (force) pipInstall.push("--force-reinstall", "--upgrade")

  const essentialArgs = [...pipInstall, ...essential]
  let depsInstalled = await run(exec, python, essentialArgs, { env })
  if (!depsInstalled.ok) {
    // 第一次失败先别报死：坏网络下最常见的不是「包不存在」，而是那个索引根本
    // 连不上。换一个能通的镜像再试一次，多半就成了。
    const index = await pickIndex({ fetch, signal, log })
    if (index !== undefined && index !== PIP_INDEXES[0]) {
      log(`必需依赖安装失败，改用镜像重试：${index}`)
      const retryArgs = [...pipInstall, "--index-url", index, "--extra-index-url", PIP_INDEXES[0], ...essential]
      depsInstalled = await run(exec, python, retryArgs, { env })
      if (depsInstalled.ok) done("依赖源", { ok: true, index })
    }
  }
  if (!depsInstalled.ok) return fail("依赖", `装必需依赖失败：${brief(depsInstalled)}`)
  done("依赖", { ok: true, backend, packages: essential.length })

  // ── 3b. 可选依赖（失败只降级，不中断）──────────────────────────────────────
  //
  // `rapidocr` / `dxcam` 都是函数内部现 import 的（`locate.py:86`、
  // `capture.py:164`），缺了服务照样起得来，只是少了 OCR 或退回慢速抓屏。
  // 它们在坏网络下恰恰最容易失败（rapidocr 会拖一串依赖），让它们挡住整个
  // 安装，用户就一个功能都用不上。
  //
  // `NO_DEPS_WIN_DEPS` 单独一步：`windows-capture` 声明依赖 `opencv-python`，
  // 和 headless 版抢同一个 `cv2`，只能 `--no-deps` 装。
  const degraded = []
  // 一个一个装。放在一条命令里，`rapidocr` 拉不下来会把 `dxcam` 一起带下水——
  // 而这两个是独立的：OCR 没了还能定位，抓屏慢了还能定位。多起两个 pip 进程
  // 只在安装/修复时发生，比「少一半功能」便宜得多。
  for (const spec of optional) {
    const opt = await run(exec, python, [...pipInstall, spec], { env })
    if (opt.ok) {
      done("可选依赖", { ok: true, package: spec })
    } else {
      degraded.push(spec)
      done("可选依赖", { ok: false, optional: true, package: spec, error: brief(opt) })
    }
  }
  if (platform === "win32" && NO_DEPS_WIN_DEPS.length) {
    const opt = await run(exec, python, [...pipInstall, "--no-deps", ...NO_DEPS_WIN_DEPS], { env })
    if (!opt.ok) degraded.push(...NO_DEPS_WIN_DEPS)
    done("可选依赖", opt.ok
      ? { ok: true, packages: NO_DEPS_WIN_DEPS }
      : { ok: false, optional: true, error: brief(opt) })
  }

  // ── 4. 本包 ────────────────────────────────────────────────────────────────
  // `--no-deps` 是有意的：上面已经把依赖按选定的后装好了，这里再让 pip 解一遍
  // 只会把后端换回 pyproject 里的默认值。
  const installArgs = ["-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "--no-deps"]
  if (force) installArgs.push("--force-reinstall", "--upgrade")
  installArgs.push(packageRoot)
  const installed = await run(exec, python, installArgs, { env })
  if (!installed.ok) return fail("本包", `装 Python 侧失败：${brief(installed)}`)
  done("本包", { ok: true, path: packageRoot })

  // ── 5. 模型 ────────────────────────────────────────────────────────────────
  //
  // 尺寸对不上就当没有：一个截断的 `.onnx` 会被推理层当成「已就绪」，然后在
  // 真正抓图时抛一句 onnxruntime 内部的话。`statSize` 拿不到尺寸时退回旧的
  // 「文件在就算好」——那是测试注入的场合，不是真实环境。
  const have = exists(plan.model) ? sizeOf(statSize, plan.model) : undefined
  if (have !== undefined && have !== MODEL_BYTES_HINT) {
    log(`模型大小异常：${have} 字节（应为 ${MODEL_BYTES_HINT}），重新下载`)
  }
  if (exists(plan.model) && !force && (have === undefined || have === MODEL_BYTES_HINT)) {
    done("模型", { ok: true, path: plan.model, reused: true, bytes: have })
  } else {
    const got = await download(MODEL_SOURCES, plan.model, {
      fetch,
      signal,
      onProgress,
      sha256: MODEL_SHA256,
      expectedBytes: MODEL_BYTES_HINT,
      measure: (path) => sizeOf(statSize, path) ?? 0,
      log,
    })
    if (!got.ok) return fail("模型", got.error)
    done("模型", {
      ok: true,
      path: plan.model,
      bytes: got.bytes,
      reused: got.reused === true,
      source: got.url === undefined ? undefined : new URL(got.url).host,
    })
  }

  // `degraded` 单独回给调用方：设置页要能显示「可用，但少了 X」，
  // 而不是把可选依赖失败当成整次安装失败。
  return { ok: true, steps, plan, degraded }
}

/** 这个文件叫这个名字，Python 侧的 `config.RUNTIME_FILE` 必须一致。 */
export const RUNTIME_FILE = "runtime.json"

/**
 * 把这次的选择写进 `<状态目录>/runtime.json`，让 Python 侧也能看见。
 *
 * ## 为什么需要这个文件
 *
 * DSH 里，设置页是 **Host 侧插件**画的，而目录最终要交给 **MCP 子进程**里的
 * Python 用。两边之间唯一的通路是 MCP 客户端的 `config.env`，但那段 `!!js`
 * 是**启动时求值一次**的 —— 用户在设置页改了模型目录，MCP 子进程不会重启，
 * 也就永远读不到新值。结果就是：设置页显示模型在 A，Python 却在默认位置找
 * 不到，报 `ModelNotFound`。
 *
 * 写文件没有这个问题：Python 每次解析路径时现读。环境变量仍然优先，所以
 * 「临时指定一下」和「在界面上选定」互不干扰。
 *
 * 写失败不算失败：最坏情况只是 Python 退回默认推导，而那正是没设过时的行为。
 *
 * @param home - 状态目录。
 * @param values - `{ modelDir, backend }`。
 * @returns 写成功与否，只用于日志。
 */
export async function writeRuntimeSettings(home, { modelDir, backend } = {}) {
  if (typeof home !== "string" || home === "") return false
  const payload = {}
  if (typeof modelDir === "string" && modelDir !== "") payload.modelDir = modelDir
  if (typeof backend === "string" && backend !== "") payload.backend = backend
  try {
    const { mkdir, writeFile } = await import("node:fs/promises")
    await mkdir(home, { recursive: true })
    await writeFile(join(home, RUNTIME_FILE), `${JSON.stringify(payload, null, 2)}\n`, "utf8")
    return true
  } catch {
    return false
  }
}

/**
 * 读回上次写下的选择。
 *
 * 写出去却读不回来，是这条通路最容易漏的一半：界面按配置里的默认值渲染，
 * 而 Python 按 `runtime.json` 里的值找模型，两边各说一套。用户上次明明把
 * 模型指到了 `%USERPROFILE%\myLocalModels\ui-locate`（那里已经有 77MB 的现成模型），
 * 界面却显示默认目录并说「模型缺失」，点一下又重新下一份。
 *
 * 容错口径与 Python 侧一致：文件不在、不是 JSON、不是对象、字段不是非空
 * 字符串，一律当没设过。
 *
 * @param home - 状态目录。
 * @returns `{ modelDir?, backend? }`，读不到就是空对象。
 */
export function readRuntimeSettings(home) {
  if (typeof home !== "string" || home === "") return {}
  try {
    const parsed = JSON.parse(readFileSync(join(home, RUNTIME_FILE), "utf8"))
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return {}
    const out = {}
    for (const field of ["modelDir", "backend"]) {
      const value = parsed[field]
      if (typeof value === "string" && value !== "") out[field] = value
    }
    return out
  } catch {
    return {}
  }
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
    // 设置页要能**在装之前**就看出这个位置有问题，所以这一项不依赖本包是否
    // 已装：探测脚本只用 ctypes，不 import ui_locate。
    integrity: { level: undefined, ui_access: false, warnings: [] },
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
    // 完整性级别只用 ctypes 读，不 import 本包：首次安装时本包还没装，而那正是
    // 最需要知道「这个位置会不会把进程降成 Low」的时刻。
    "rid=None",
    "ua=False",
    "try:",
    "    import ctypes, ctypes.wintypes as w",
    "    k=ctypes.WinDLL('kernel32', use_last_error=True)",
    "    a=ctypes.WinDLL('advapi32', use_last_error=True)",
    "    k.GetCurrentProcess.restype=w.HANDLE",
    "    k.CloseHandle.argtypes=[w.HANDLE]",
    "    a.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,ctypes.POINTER(w.HANDLE)]",
    "    a.GetTokenInformation.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD)]",
    "    a.GetSidSubAuthorityCount.argtypes=[ctypes.c_void_p]",
    "    a.GetSidSubAuthorityCount.restype=ctypes.POINTER(ctypes.c_ubyte)",
    "    a.GetSidSubAuthority.argtypes=[ctypes.c_void_p,w.DWORD]",
    "    a.GetSidSubAuthority.restype=ctypes.POINTER(w.DWORD)",
    "    t=w.HANDLE()",
    "    if a.OpenProcessToken(k.GetCurrentProcess(),0x0008,ctypes.byref(t)):",
    "        try:",
    "            n=w.DWORD(0)",
    "            a.GetTokenInformation(t,25,None,0,ctypes.byref(n))",
    "            buf=ctypes.create_string_buffer(max(n.value,64))",
    "            if a.GetTokenInformation(t,25,buf,len(buf),ctypes.byref(n)):",
    "                sid=ctypes.c_void_p.from_buffer(buf).value",
    "                cnt=a.GetSidSubAuthorityCount(sid)[0]",
    "                rid=int(a.GetSidSubAuthority(sid,cnt-1)[0])",
    "            u=w.DWORD(0)",
    "            if a.GetTokenInformation(t,26,ctypes.byref(u),ctypes.sizeof(u),ctypes.byref(n)):",
    "                ua=bool(u.value)",
    "        finally:",
    "            k.CloseHandle(t)",
    "except Exception: pass",
    `print(json.dumps({"python":sys.version.split()[0],"pkg":v,"backend":b,"rid":rid,"ui_access":ua}))`,
  ].join("\n")
  const result = await run(exec, plan.python, ["-c", probe])
  if (result.ok) {
    try {
      const parsed = JSON.parse(result.stdout.trim().split("\n").pop())
      facts.pythonVersion = parsed.python ?? undefined
      facts.installedVersion = parsed.pkg ?? undefined
      facts.backend = parsed.backend ?? undefined
      facts.integrity = integrityFacts(parsed.rid, parsed.ui_access === true)
    } catch {
      /* 输出不是 JSON 就只报「venv 在」这一层 */
    }
  }
  return facts
}
