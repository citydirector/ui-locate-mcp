/**
 * 更新逻辑里与框架无关的那部分：版本比较、发布源读取、venv 同步、状态机。
 *
 * 单独成文件的原因是可测——这里一行 Cordis 都没有，测试可以直接用真 node
 * 跑，不需要给 `@deepseek-ai/dsh-tools` 造桩。`index.mjs` 只负责把它接到
 * Host 上。
 *
 * 所有会碰外部世界的东西（`fetch`、`execFile`、时钟）都从参数注入，测试
 * 因此不需要开端口、不需要真的装包。
 */

import { execFile } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"

/** 本插件发布所在的仓库。 */
export const UPDATE_REPOSITORY = "citydirector/ui-locate-mcp"

/** Python 包名，用来问 venv 里装的是哪个版本。 */
export const PYTHON_PACKAGE = "ui-locate-mcp"

/**
 * 只认 `vX.Y.Z`。
 *
 * 预发布标签故意不认：自动装的东西必须是确定的，`-rc.1` 这种随时会被替换。
 */
const RELEASE_TAG = /^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/

// ── 版本比较 ───────────────────────────────────────────────────────────────

/**
 * 读一个发布标签。
 *
 * @param tag - git 标签名。
 * @returns `[major, minor, patch]`；不是 `vX.Y.Z` 时返回 undefined。
 */
export function parseReleaseTag(tag) {
  const match = RELEASE_TAG.exec(String(tag ?? "").trim())
  return match === null ? undefined : [Number(match[1]), Number(match[2]), Number(match[3])]
}

/**
 * 比较两个版本号，写 `X.Y.Z` 或 `vX.Y.Z` 都行。
 *
 * @returns 负数、零、正数，行为同排序比较器；解析不出来的排在后面。
 */
export function compareVersions(a, b) {
  const tag = (value) => (String(value ?? "").startsWith("v") ? String(value) : `v${value}`)
  const pa = parseReleaseTag(tag(a))
  const pb = parseReleaseTag(tag(b))
  if (pa === undefined || pb === undefined) {
    // 解析不出来的排到后面：调用方是在挑「最新版本」，认不出的标签不该胜出。
    if (pa === undefined && pb === undefined) return 0
    return pa === undefined ? 1 : -1
  }
  for (let i = 0; i < 3; i += 1) if (pa[i] !== pb[i]) return pa[i] - pb[i]
  return 0
}

/**
 * 一组标签里最新的那个发布版本。
 *
 * @param tags - 标签名列表。
 * @returns 最新标签名，或 undefined（一个 `vX.Y.Z` 都没有）。
 */
export function latestRelease(tags) {
  let best
  for (const tag of tags) {
    if (parseReleaseTag(tag) === undefined) continue
    if (best === undefined || compareVersions(tag, best) > 0) best = tag
  }
  return best
}

/**
 * 把标签列表和当前版本比一下。
 *
 * @param tags - 发布源里的标签名。
 * @param current - 本包正在跑的版本（`X.Y.Z`）。
 * @returns 有更新时 `{ state: "available", latest }`，否则 `{ state: "current", latest? }`。
 */
export function updateVerdict(tags, current) {
  const latest = latestRelease(tags)
  if (latest !== undefined && compareVersions(latest, current) > 0) return { state: "available", latest }
  return { state: "current", ...(latest !== undefined ? { latest } : {}) }
}

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
 * 问 venv 里装的 Python 包是哪个版本。
 *
 * @param python - venv 解释器绝对路径。
 * @param options - `{ exec, exists }` 注入点，测试用。
 * @returns 版本字符串；venv 不存在或包没装时 undefined。
 */
export function installedPythonVersion(python, { exec = execFile, exists = existsSync } = {}) {
  if (!exists(python)) return Promise.resolve(undefined)
  return new Promise((resolve) => {
    exec(
      python,
      ["-c", `import importlib.metadata as m; print(m.version(${JSON.stringify(PYTHON_PACKAGE)}))`],
      { timeout: 20000 },
      (error, stdout) => {
        if (error) {
          resolve(undefined)
          return
        }
        const value = String(stdout ?? "").trim()
        resolve(value === "" ? undefined : value)
      },
    )
  })
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
    new Promise((resolve, reject) => {
      exec(
        command,
        args,
        { timeout: 900000, env: { ...process.env, GIT_TERMINAL_PROMPT: "0" } },
        (error, stdout, stderr) => {
          if (error) reject(Object.assign(new Error(String(error.message)), { output: String(stderr ?? "").slice(-2000) }))
          else resolve(stdout)
        },
      )
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
  const target = root.replace(/[\\/]+$/, "")
  try {
    await installIntoVenv(python, target, { exec, noDeps: true })
    const after = await installedPythonVersion(python, { exec, exists })
    return after === wanted ? { state: "updated", version: after } : { state: "mismatch", have: after, wanted }
  } catch (error) {
    return { state: "failed", error: String(error?.message ?? error).split("\n")[0].slice(0, 200), wanted }
  }
}

// ── 发布源 ─────────────────────────────────────────────────────────────────

/**
 * 读 GitHub 的 tag 列表。
 *
 * 只读公开仓库、不带凭据：这里没有可泄露的东西，也不需要用户配 token。
 *
 * @param options - `{ repository, api, fetch, signal }`，测试用。
 * @returns 标签名数组。
 */
export async function fetchReleaseTags({
  repository = UPDATE_REPOSITORY,
  api = "https://api.github.com",
  fetch: request = fetch,
  signal,
} = {}) {
  const response = await request(`${api.replace(/\/+$/, "")}/repos/${repository}/tags?per_page=100`, {
    headers: {
      accept: "application/vnd.github+json",
      "user-agent": "ui-locate-mcp",
      "x-github-api-version": "2022-11-28",
    },
    signal,
  })
  if (!response.ok) {
    const limited = response.status === 403 || response.status === 429
    throw new Error(limited ? `GitHub rate limit (HTTP ${response.status})` : `GitHub answered HTTP ${response.status}`)
  }
  const body = await response.json()
  if (!Array.isArray(body)) throw new Error("GitHub answered something other than a tag list")
  return body.map((item) => item?.name).filter((value) => typeof value === "string")
}

// ── 更新状态机 ─────────────────────────────────────────────────────────────

/**
 * 一个只在内存里的更新跟踪器：记当前状态、做检查、排出下一次检查。
 *
 * 和框架无关，所以能完整测到（包括失败路径和时间安排）。它**从不安装任何
 * 东西**——安装是 `index.mjs` 在收到显式工具调用时才做的事。
 *
 * @param options - `{ readVersion, fetchTags, delayMs, intervalMs, now, log, timeoutMs }`。
 */
export function createUpdateTracker({
  readVersion = () => packageVersion(),
  fetchTags = (options) => fetchReleaseTags(options),
  delayMs = 8000,
  intervalMs = 12 * 60 * 60 * 1000,
  now = () => Date.now(),
  log = () => {},
  timeoutMs = 20000,
} = {}) {
  /** state ∈ off | pending | checking | current | available | failed */
  let update = { state: "pending" }
  let running
  let delayTimer
  let intervalTimer
  const abort = new AbortController()

  const stamp = () => new Date(now()).toISOString()

  return {
    /** 当前状态的一份快照（调用方可以随便改，不影响内部）。 */
    get state() {
      return { ...update }
    },

    /** 一次检查。已有检查在跑时复用同一个 Promise，不叠加请求。 */
    check() {
      if (running !== undefined) return running
      const version = readVersion()
      if (version === undefined) {
        update = { state: "failed", error: "own version unreadable", checkedAt: stamp() }
        return Promise.resolve(update)
      }
      const previous = update
      update = { ...previous, state: "checking" }
      const signal = AbortSignal.any([abort.signal, AbortSignal.timeout(timeoutMs)])
      running = fetchTags({ signal })
        .then(
          (tags) => {
            update = { ...updateVerdict(tags, version), checkedAt: stamp() }
            log(`update check: ${update.state}${update.latest ? ` (newest ${update.latest}, running ${version})` : ""}`)
          },
          (error) => {
            if (abort.signal.aborted) return
            const message =
              error?.name === "TimeoutError" || error?.name === "AbortError"
                ? "timed out"
                : String(error?.message ?? error).split("\n")[0].slice(0, 200)
            // 保留上次查到的版本：一次网络抖动不该把已知的新版本藏起来。
            update = {
              ...(previous.latest !== undefined ? { latest: previous.latest } : {}),
              state: "failed",
              error: message,
              checkedAt: stamp(),
              ...(previous.state === "available" ? { stale: "available" } : {}),
            }
            log(`update check failed: ${message}`)
          },
        )
        .then(() => update)
        .finally(() => {
          running = undefined
        })
      return running
    },

    /**
     * 跟着开关走：开着就排期，关了就把 timer 清掉。
     *
     * @param enabled - 是否继续检查。
     */
    schedule(enabled) {
      if (!enabled) {
        clearTimeout(delayTimer)
        clearInterval(intervalTimer)
        delayTimer = undefined
        intervalTimer = undefined
        update = { state: "off" }
        return
      }
      if (delayTimer !== undefined || intervalTimer !== undefined) return
      if (update.state === "off") update = { state: "pending" }
      delayTimer = setTimeout(() => {
        delayTimer = undefined
        void this.check()
        intervalTimer = setInterval(() => void this.check(), intervalMs)
        intervalTimer.unref?.()
      }, delayMs)
      delayTimer.unref?.()
    },

    /** 停掉一切：清 timer、掐断在飞的请求。 */
    dispose() {
      abort.abort()
      clearTimeout(delayTimer)
      clearInterval(intervalTimer)
      delayTimer = undefined
      intervalTimer = undefined
    },

    /**
     * 一页能看懂的当前状态。
     *
     * 早先查到的「有更新」可能已经装上了，所以按当前版本再过一遍。
     *
     * @param options - `{ python }`：`{ wanted, installed, venv }`。
     */
    document({ python } = {}) {
      const version = readVersion()
      const doc = {
        version,
        repository: UPDATE_REPOSITORY,
        update: { ...update },
      }
      if (python !== undefined) doc.python = python
      const settled = version !== undefined && doc.update.latest !== undefined && compareVersions(doc.update.latest, version) <= 0
      if (settled && doc.update.state === "available") doc.update.state = "current"
      if (settled && doc.update.stale === "available") delete doc.update.stale
      // 只有真能装上时才给出安装 spec：已经是最新版了还留一个「去装这个」
      // 的地址，会让 UI 显示一个点了没有意义的按钮。
      if (doc.update.state === "available" && doc.update.latest !== undefined) {
        doc.update.spec = `git+https://github.com/${UPDATE_REPOSITORY}.git#${doc.update.latest}`
      } else if (doc.update.stale === "available" && doc.update.latest !== undefined) {
        doc.update.spec = `git+https://github.com/${UPDATE_REPOSITORY}.git#${doc.update.latest}`
      }
      return doc
    },
  }
}
