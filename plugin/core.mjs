/**
 * 与框架无关的那部分：读本包元数据、算运行目录、问 venv 里的 Python 版本、
 * 把 Python 侧同步到包内声明的版本。
 *
 * 单独成文件的原因是可测——这里一行 Cordis 都没有，测试可以直接用真 node
 * 跑，不需要给宿主包造桩。`index.mjs` 只负责把它接到 Host 上。
 *
 * 所有会碰外部世界的东西（`execFile`）都从参数注入，测试因此不需要真的
 * 装包。
 *
 * ## 这里**没有**更新检查
 *
 * 插件的新版本由 DSH 插件页自己管：装上 registry 包之后，那一页就会给每个
 * 三方可更新插件显示更新按钮，走的是宿主自己的 `pluginManager`。我们不再
 * 自己读版本列表、也不再自己装——两套并行的更新链路只会互相打架（各自认为
 * 什么是最新的，还能把对方的依赖写法改掉）。
 *
 * 保留下来的只有**版本同步**：npm 包换新之后，venv 里那份 Python 包还是旧的，
 * 这一层宿主不管，必须由我们自己补上。
 */

import { execFile } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"

/** Python 包名，用来问 venv 里装的是哪个版本。 */
export const PYTHON_PACKAGE = "ui-locate-mcp"


// ── 读本包自己的元数据 ──────────────────────────────────────────────────────

/**
 * 读包的版本号。
 *
 * 每次调用都重新读，不缓存：热替换之后 `../package.json` 已经是新内容，
 * 缓存会让新版本误以为自己还是旧的。
 *
 * @param root - 包根目录，默认从本文件位置推。
 * @returns 版本字符串，读不到时 undefined。
 */
export function packageVersion(root = fileURLToPath(new URL("..", import.meta.url))) {
  try {
    const pkg = JSON.parse(readFileSync(`${root.replace(/[\\/]+$/, "")}/package.json`, "utf8"))
    return typeof pkg.version === "string" ? pkg.version : undefined
  } catch {
    return undefined
  }
}

/**
 * 读包内 `pyproject.toml` 声明的版本——也就是「这个 bundle 想要的 Python 版本」。
 *
 * 只在 `[project]` 段里找，避免误抓到 `[tool.*]` 下的版本号。
 *
 * @param root - 包根目录。
 * @returns 版本字符串，读不到时 undefined。
 */
export function bundledPythonVersion(root = fileURLToPath(new URL("..", import.meta.url))) {
  try {
    const text = readFileSync(`${root.replace(/[\\/]+$/, "")}/pyproject.toml`, "utf8")
    let inProject = false
    for (const line of text.split("\n")) {
      const section = /^\s*\[([^\]]+)\]\s*$/.exec(line)
      if (section !== null) {
        inProject = section[1].trim() === "project"
        continue
      }
      if (!inProject) continue
      const match = /^\s*version\s*=\s*"([^"]+)"/.exec(line)
      if (match !== null) return match[1]
    }
  } catch {
    /* 读不到就当未知 */
  }
  return undefined
}

// ── 运行环境位置 ───────────────────────────────────────────────────────────

/**
 * 运行环境的目录，与 `cordis.patch.yml` 里的 `!!js` 表达式同一套优先级：
 * `UI_LOCATE_HOME` → `$DSH_HOME/ui-locate` → `%LOCALAPPDATA%/ui-locate` → 当前目录。
 *
 * Host 进程能看见 `DSH_HOME`；MCP 子进程看不到（`DSH_` 前缀会被清洗），
 * 所以那边是把算好的值经 `config.env` 传进去的。
 *
 * @param env - 环境变量快照。
 * @returns 绝对路径。
 */
export function stateDir(env = process.env) {
  if (env.UI_LOCATE_HOME) return env.UI_LOCATE_HOME
  if (env.DSH_HOME) return `${env.DSH_HOME}/ui-locate`
  const base = env.LOCALAPPDATA ?? (env.HOME ? `${env.HOME}/.local/share` : undefined)
  return base === undefined ? `${process.cwd()}/ui-locate` : `${base}/ui-locate`
}

/**
 * venv 里的解释器路径，按平台猜（Windows 是 Scripts，其余是 bin）。
 *
 * @param home - 运行环境根目录。
 * @param platform - 目标平台，默认当前平台。
 */
export function venvPython(home, platform = process.platform) {
  return platform === "win32" ? `${home}/venv/Scripts/python.exe` : `${home}/venv/bin/python`
}

// ── Python 侧 ──────────────────────────────────────────────────────────────

/**
 * 跑一次子进程，把「同步抛」也折成 rejected Promise。
 *
 * `execFile` 有两种失败姿势：
 *   1. 进回调：进程起来了但退出码非零；
 *   2. **同步 throw**：根本没起来（`spawn EPERM`、可执行文件不存在）。
 *
 * 第 2 种在 `new Promise(executor)` 里抛，会直接冒出 Promise 构造器 —— 调用
 * 方拿到的是**同步异常**而不是 rejected Promise。这个差别在 `void fn()` 的
 * 场合是致命的：`void` 接不住同步异常，它会把调用栈一路炸到 `apply()`，
 * 于是插件加载失败、Host 半身整个不出现（实测过一次，见 README 坑 33）。
 *
 * 所以凡是「可能起不来」的子进程，都必须经过这里。
 */
function runProcess(exec, command, args, options) {
  return new Promise((resolve, reject) => {
    // 这里的 try 是保险，不是必需：这个 Promise 本身就带 reject，exec 同步抛
    // 时 Promise 构造器会接住并把它变成 rejected —— 与下面 catch 里的
    // reject(error) 等价。实测拆掉它测试不变红，就是这个原因。
    // runtime.mjs 的 run() 不一样：那个 Promise 只 resolve 不 reject，拆掉
    // try 就真的会漏出去。两个文件的差别是实的，别照着一个改另一个。
    try {
      exec(command, args, options, (error, stdout, stderr) => {
        if (error) {
          reject(Object.assign(new Error(String(error.message)), {
            output: String(stderr ?? "").slice(-2000),
          }))
          return
        }
        resolve(stdout)
      })
    } catch (error) {
      // spawn 层面的失败：ENOENT / EPERM / EACCES 都走这里
      reject(error)
    }
  })
}

/**
 * 问 venv 里装的 Python 包是哪个版本。
 *
 * @param python - venv 解释器绝对路径。
 * @param options - `{ exec, exists }` 注入点，测试用。
 * @returns 版本字符串；venv 不存在或包没装时 undefined。
 */
export function installedPythonVersion(python, { exec = execFile, exists = existsSync } = {}) {
  if (!exists(python)) return Promise.resolve(undefined)
  return runProcess(
    exec,
    python,
    ["-c", `import importlib.metadata as m; print(m.version(${JSON.stringify(PYTHON_PACKAGE)}))`],
    { timeout: 20000 },
  ).then(
    (stdout) => {
      const value = String(stdout ?? "").trim()
      return value === "" ? undefined : value
    },
    () => undefined,
  )
}

/**
 * 跑一次安装，优先用 `uv`（快，且和 `scripts/bootstrap.ps1` 的选择一致），
 * 没有 `uv` 就退回 venv 自己的 `python -m pip`。
 *
 * @param python - venv 解释器绝对路径。
 * @param target - 要安装的路径。
 * @param options - `{ exec, noDeps }` 注入点，测试用。
 * @returns 成功时 undefined，失败时抛错。
 */
export function installIntoVenv(python, target, { exec = execFile, noDeps = false } = {}) {
  const run = (command, args) =>
    runProcess(exec, command, args, {
      timeout: 900000,
      env: { ...process.env, GIT_TERMINAL_PROMPT: "0" },
    })
  const extra = noDeps ? ["--no-deps"] : []
  return run("uv", ["pip", "install", "--python", python, ...extra, target]).catch((error) => {
    // uv 不在 PATH 时退回 pip。别把「没有 uv」当成「装不上」：很多机器上就是没装。
    if (/\bENOENT\b|not found|not recognized/i.test(String(error?.message ?? ""))) {
      return run(python, ["-m", "pip", "install", "--upgrade", ...extra, target])
    }
    return Promise.reject(error)
  })
}

/**
 * 把 venv 里的 Python 包同步到包内声明的版本。
 *
 * 触发条件是「两边版本不一致」，不是「刚更新过」——所以它天然自愈：上一次
 * 失败留下的不一致，下一次启动还会被发现。
 *
 * venv 不存在时什么都不做并说明原因。建 venv 要下 77MB 模型，那是
 * `scripts/bootstrap.ps1` 的职责，不该在插件启动时悄悄做。
 *
 * **只换代码，不动后端。** 用 `--no-deps` 装本包：运行时依赖（含 ORT 的哪
 * 一个 wheel）是 bootstrap 时按用户选的，同步版本时不该擅自把它们换掉 ——
 * 三个 ORT wheel 都提供同一个 `onnxruntime` 顶层包，重装一次就可能把用户的
 * CUDA 换成 DirectML，或者反过来，而且不报错。
 *
 * @param options - `{ root, home, platform, exec, exists }`，测试用。
 * @returns `{ state, ... }`，state ∈ missing | unknown | current | updated | mismatch | failed
 */
export async function syncPythonToBundle({
  root = fileURLToPath(new URL("..", import.meta.url)),
  home = stateDir(),
  platform = process.platform,
  exec = execFile,
  exists = existsSync,
} = {}) {
  const python = venvPython(home, platform)
  const wanted = bundledPythonVersion(root)
  if (!exists(python)) return { state: "missing", home, python, wanted }
  if (wanted === undefined) return { state: "unknown", reason: "pyproject.toml unreadable" }
  const have = await installedPythonVersion(python, { exec, exists })
  if (have === wanted) return { state: "current", version: have }

  // 读不到版本有两种可能：包真没装（重装有用），或者解释器根本跑不起来
  // （重装没用，会白等一次安装超时）。后者要单独报，别让它伪装成「装上了
  // 但版本不对」—— 那会把排查引到错误的方向上。
  if (have === undefined && !(await canRunPython(python, { exec }))) {
    return { state: "broken", home, python, wanted, reason: "venv 里的解释器跑不起来" }
  }

  const target = root.replace(/[\\/]+$/, "")
  try {
    await installIntoVenv(python, target, { exec, noDeps: true })
    const after = await installedPythonVersion(python, { exec, exists })
    if (after === wanted) return { state: "updated", version: after }
    if (after === undefined) return { state: "failed", error: "装完之后仍然读不到版本", wanted }
    return { state: "mismatch", have: after, wanted }
  } catch (error) {
    return { state: "failed", error: String(error?.message ?? error).split("\n")[0].slice(0, 200), wanted }
  }
}

/**
 * venv 里的解释器能不能跑起来。
 *
 * 只要它能打印一行东西就算活着 —— 这里问的不是版本，是「这个 exe 还能不能
 * 被执行」。区分它和不区分它，决定了同步逻辑会不会在一个已经坏掉的 venv
 * 上空转一次安装超时。
 */
async function canRunPython(python, { exec }) {
  try {
    const out = await runProcess(exec, python, ["-c", "print(1)"], { timeout: 20000 })
    return String(out ?? "").trim().endsWith("1")
  } catch {
    return false
  }
}
