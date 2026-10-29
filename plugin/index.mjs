/**
 * ui-locate 的 Host 侧插件：查新版本、装新版本、准备 Python 运行环境。
 *
 * ## 为什么 ui-locate 需要一个 Host 插件
 *
 * 这个 bundle 的主体是一个 MCP 客户端行（`cordis.patch.yml` 的第一行），
 * 它只声明「用什么命令起 Python 进程」，本身没有可执行代码。DSH 的插件
 * 管理器能装 JS/YAML 那部分，但**装不到 venv 里的 Python 包**——而那才是
 * 真正会改进的部分（算法、模型、OCR 参数）。所以需要一段 Host 代码同时
 * 管住两边。
 *
 * ## 三件事
 *
 * 1. **准备运行环境**（`runtime.mjs`）：venv、依赖、模型。用 DSH 自带的
 *    Python，不要求用户先装 uv，也不需要终端。
 * 2. **界面**（`api.mjs` + `client.js`）：设置里一页「运行环境」，能看见
 *    装到哪一步、装在哪，并且能一键重来。
 * 3. **自我更新**（`core.mjs`）：查 GitHub 的发布标签，装新版本，然后在
 *    新一代实例启动时把 Python 侧同步过去。
 *
 * ## 更新是两段，顺序不能颠倒
 *
 * 1. **JS 侧**：`pluginManager.installBundle('git+<url>#vX.Y.Z')`，DSH 用它
 *    装官方插件的方式装本包，落到 profile 的 node_modules，然后热替换本
 *    插件（本实例会被新的取代）。
 * 2. **Python 侧**：新版本的源码这时才出现在 node_modules 里，所以只能在
 *    **换上来之后**执行。第 2 步由「新实例启动时发现版本不一致」触发，
 *    不塞进 install 的同一段流程——那样会拿旧路径去装新版本。
 *
 * 这个顺序也给了容错：Python 侧失败不影响 JS 侧已经装好，下次启动会重新
 * 发现不一致并再试一次，不需要用户干预。
 *
 * ## 只查不装
 *
 * `updateCheck` 默认开着，但检查本身**从不安装任何东西**——它只读 GitHub
 * 的 tag 列表并记下结论。安装必须由一次显式的工具调用或界面上的按钮触发。
 *
 * 纯逻辑都在 `core.mjs` 与 `runtime.mjs`：那两个文件一行 Cordis 都没有，
 * 可以直接用 node 测。
 */

import { existsSync, statSync } from "node:fs"
import { join } from "node:path"
import { fileURLToPath } from "node:url"
import { execFile } from "node:child_process"
import z from "@deepseek-ai/schemastery"
import { defineTool, ToolArgsError } from "./tool.mjs"
import {
  NPM_PACKAGE,
  bundledPythonVersion,
  compareVersions,
  createUpdateTracker,
  fetchPublishedVersions,
  installedPythonVersion,
  packageVersion,
  stateDir,
  syncPythonToBundle,
  venvPython,
} from "./core.mjs"
import {
  DEFAULT_BACKEND,
  bundledPythonCandidates,
  ensureRuntime,
  inspectRuntime,
  runtimePlan,
  writeRuntimeSettings,
  readRuntimeSettings,
} from "./runtime.mjs"
import { createApi } from "./api.mjs"

export const name = "ui-locate-update"

/**
 * `tools` 必给：本插件要注册两个工具。
 *
 * `pluginManager` 与 `webServer` **刻意不写进 inject**：都是可选能力，
 * 写进去会让整个插件在没有它们的部署里直接起不来，连查版本都做不了。
 * 改为在 apply 里用 `ctx.inject([...])` 按需拿。
 */
export const inject = ["tools"]

/** 包根目录：`plugin/` 的上一层，也就是被装进 node_modules 的那个目录。 */
const PACKAGE_ROOT = fileURLToPath(new URL("..", import.meta.url))

export const Config = z.object({
  updateCheck: z
    .boolean()
    .default(true)
    .volatile()
    .description("启动几秒后查一次 GitHub 上的新版本，之后每 12 小时一次。只检查，不安装。"),
  pythonSync: z
    .boolean()
    .default(true)
    .volatile()
    .description("发现 venv 里的 Python 包版本与包内 pyproject.toml 不一致时，自动重装到 venv。"),
  runtimeDir: z
    .string()
    .default("")
    .volatile()
    .description("Python 环境与日志的存放目录。留空则跟随 DSH 数据目录（便携版在 data/ui-locate）。"),
  modelDir: z
    .string()
    .default("")
    .volatile()
    .description("检测模型的存放目录。留空则跟随运行目录下的 models/。已有模型可以指过来，省一次 77MB 下载。"),
  backend: z
    .string()
    .default("")
    .volatile()
    .description(
      "推理后端：directml（默认，通用）或 cuda（仅 NVIDIA，快一点但多 1.7GB）或 cpu。留空则沿用设置页里选过的那个。",
    ),
})

/**
 * 挂载更新检查、运行环境准备与设置页接口。
 *
 * @param ctx - 插件上下文（`tools` 已注入）。
 * @param config - schema 解析后的 Config：每个字段一个 volatile 引用。
 */
export function apply(ctx, config) {
  /** 最近一次读到的配置值。 */
  let current
  /** 插件管理器，拿到之后才能安装；没有就只报告。 */
  let manager
  /** webServer，拿到之后才有设置页接口；没有就只留工具。 */
  let server

  const tracker = createUpdateTracker({
    // 测试与自建镜像都可以改这个地址；默认打公开的 GitHub。
    // 读 npm registry 上的版本，不再读 GitHub 标签：DSH 的更新按钮只认
    // registry 包，我们的更新器也必须跟着同一条源，否则它升一次就把依赖
    // 写回 github: 形式，按钮又没了。
    fetchTags: ({ signal }) => fetchPublishedVersions({ signal }),
    delayMs: readDelay(),
    log: (line) => log(line),
  })

  function log(line) {
    try {
      ctx.logger?.info?.("ui-locate: %s", line)
    } catch {
      /* 记日志失败不该影响功能 */
    }
  }

  function readDelay() {
    const value = Number(process.env.DSH_UI_LOCATE_UPDATE_DELAY_MS)
    return Number.isFinite(value) && value >= 0 ? value : 8000
  }

  /** 一次 `.get()` 读一个字段；缺省字段用 schema 默认值。 */
  function readConfig() {
    const parsed = config ?? Config({})
    const values = {}
    for (const field of ["updateCheck", "pythonSync", "runtimeDir", "modelDir", "backend"]) {
      const ref = parsed[field]
      values[field] = ref !== undefined && typeof ref.get === "function" ? ref.get() : ref
    }
    current = values
  }

  /**
   * 当前该用哪些位置。
   *
   * 每次现算而不是缓存：用户可能在设置页里刚改完就点安装，配置刷新是异步的。
   * `defaultHome` 由 `stateDir()` 决定，也就是 `UI_LOCATE_HOME` →
   * `DSH_HOME/ui-locate` → `LOCALAPPDATA/ui-locate` 那条链。
   */
  function plan(override = {}) {
    const defaults = stateDir()
    // 优先级：本次显式传入 → 插件配置 → 上次写下的 runtime.json → 默认值。
    //
    // 之所以要读回 runtime.json：这个文件的用途是「告诉 Python 侧用哪个目录」，
    // 而它同时也是**用户上次在这里选过什么**的唯一记录。不读回来的话，界面
    // 永远按默认目录渲染，而 Python 按文件里的目录找模型，两边各说一套——
    // 用户明明把模型指到了现成的那一份，界面却说「模型缺失」，点一下又重新
    // 下载 77MB。
    const remembered = readRuntimeSettings(defaults)
    const resolved = runtimePlan({
      home: override.home ?? current?.runtimeDir ?? "",
      // 这里必须用 `||` 而不是 `??`：插件配置的两个字段默认值是**空串**，
      // 而 `"" ?? x` 得到的是 `""`——`??` 只在 null/undefined 时才让位。
      modelDir: override.modelDir || current?.modelDir || remembered.modelDir || "",
      defaultHome: defaults,
      defaultModelDir: join(defaults, "models"),
    })
    return {
      ...resolved,
      backend: override.backend || current?.backend || remembered.backend || DEFAULT_BACKEND,
      defaultHome: defaults,
      defaultModelDir: join(defaults, "models"),
      venvPython: resolved.python,
    }
  }

  /**
   * 递归去掉值为 `undefined` 的键。
   *
   * 工具返回值要过 `validateJsonSchemaValue` 的「无损 JSON」检查，而
   * `undefined` 不是合法 JSON 值——只要有一处漏出来，整个返回值就被判成
   * `INVALID_TOOL_OUTPUT`，工具调用直接失败。而这个返回值里到处是「可能
   * 读不到」的东西：版本号、venv 里的包版本、上次查到的最新标签、安装
   * 结果里的 `application` 与 `restartReason`。所以与其在每个赋值处记得
   * 判断，不如在出口统一扫一遍。
   *
   * 数组里的元素保留位置（`undefined` 变成 `null`），因为数组的下标是有
   * 意义的；对象里的键直接删掉，让字段自然缺席。
   *
   * @param value - 任意 JSON 形状的值。
   * @returns 不含 `undefined` 的同构值。
   */
  function lossless(value) {
    if (Array.isArray(value)) return value.map((item) => (item === undefined ? null : lossless(item)))
    if (value === null || typeof value !== "object") return value
    const out = {}
    for (const [key, item] of Object.entries(value)) {
      if (item !== undefined) out[key] = lossless(item)
    }
    return out
  }

  /** venv 里 Python 侧的三项事实，给状态文档用。 */
  async function pythonFacts() {
    const home = plan().home
    const python = venvPython(home)
    return {
      wanted: bundledPythonVersion(),
      installed: await installedPythonVersion(python),
      venv: existsSync(python),
      home,
    }
  }

  /** 一页能看懂的当前状态，工具的返回值就是这个。 */
  async function statusDocument() {
    const python = await pythonFacts()
    return { ...tracker.document({ python }), home: python.home }
  }

  /** 把 Python 侧同步到包内声明的版本；`pythonSync` 关掉时不动。 */
  function syncPython({ force = false } = {}) {
    if (!force && current?.pythonSync === false) return Promise.resolve({ state: "off" })
    return syncPythonToBundle().then(
      (result) => {
        if (result.state !== "current" && result.state !== "missing") {
          log(`python sync: ${JSON.stringify(result)}`)
        }
        return result
      },
      // 兜底：这一步是自愈性的附加动作，失败了下次启动还会再试。
      // **绝不能让它的失败影响插件加载** —— 宿主是拿「apply 有没有跑完」
      // 判断这个插件活没活的，一次同步失败不该把整个 Host 半身带走。
      (error) => {
        log(`python sync 失败: ${error?.message ?? error}`)
        return { state: "failed", error: String(error?.message ?? error) }
      },
    )
  }

  /** 只读地看一眼运行环境，给工具与设置页共用。 */
  async function inspect(target = plan()) {
    const facts = await inspectRuntime({
      home: target.home,
      modelDir: target.modelDir,
      packageName: "ui-locate-mcp",
      exec: execFile,
      exists: existsSync,
      statSize: (path) => {
        try {
          return statSync(path).size
        } catch {
          return undefined
        }
      },
    })
    return { ...facts, backend: facts.backend ?? undefined, wantedBackend: target.backend }
  }

  /**
   * 真正去准备环境。设置页与工具都走这一条。
   *
   * @param target - `plan()` 的结果（可能带用户的覆盖）。
   * @returns `ensureRuntime` 的结论，加上计划本身。
   */
  async function prepare(target, { force = false, onProgress } = {}) {
    const report = (step, detail) => {
      onProgress?.(step, detail)
    }
    report("查找 Python", "")
    const pythonExe = pickPython()
    if (pythonExe === undefined) {
      return { ok: false, error: "找不到 DSH 自带的 Python 运行时", steps: [] }
    }
    report("准备中", "venv → 依赖 → 模型")
    const result = await ensureRuntime({
      home: target.home,
      modelDir: target.modelDir,
      pythonExe,
      packageRoot: PACKAGE_ROOT,
      backend: target.backend,
      force,
      exec: execFile,
      onProgress: (written, total) => {
        const mb = (written / (1 << 20)).toFixed(1)
        report("下载模型", total > 0 ? `${mb} / ${(total / (1 << 20)).toFixed(1)} MB` : `${mb} MB`)
      },
      log: (line) => log(line),
    })
    // 让 Python 侧也知道这次选在哪。MCP 子进程读不到设置页的配置（那段
    // `!!js` 只在启动时求值一次），只能靠这个文件传话。
    if (result.ok) {
      await writeRuntimeSettings(target.home, { modelDir: target.modelDir, backend: target.backend })
    }
    return { ...result, plan: target }
  }

  /** DSH 自带的 Python。找不到就返回 undefined，让上层报一句人话。 */
  function pickPython() {
    const candidates = [process.env.UI_LOCATE_PYTHON, ...bundledPythonCandidates(process.execPath)]
    for (const candidate of candidates) {
      if (typeof candidate === "string" && candidate !== "" && existsSync(candidate)) return candidate
    }
    return undefined
  }

  /**
   * 装一个新版本。
   *
   * 只做 JS 侧，Python 侧交给新一代实例的启动钩子（见文件头）。这样失败点
   * 只有一个，也不需要旧实例去猜新版本的路径。
   */
  async function installLatest() {
    if (manager === undefined) return { ok: false, error: "plugin manager unavailable" }
    const state = tracker.state
    if (state.latest === undefined) await tracker.check()
    const settled = tracker.state
    if (settled.latest === undefined) {
      return { ok: false, error: settled.error ?? "no release tag found" }
    }
    const tag = settled.latest
    const running = packageVersion()
    if (running !== undefined && compareVersions(tag, running) <= 0) {
      return { ok: true, state: "current", version: running }
    }
    // registry 形式：`ui-locate-mcp@0.4.1`。
    // 不能用 `git+https://...#vX.Y.Z` —— 那样装完依赖会变成 `github:...`，
    // DSH 插件页的更新按钮随即消失（`updateRefusal()` 要求 kind === "registry"）。
    const spec = `${NPM_PACKAGE}@${tag.replace(/^v/, "")}`
    log(`installing ${spec}`)
    const requestId = globalThis.crypto?.randomUUID?.() ?? `ui-locate-${Date.now()}`
    let answer
    try {
      answer = await manager.installBundle(spec, { requestId })
    } catch (error) {
      return { ok: false, error: String(error?.message ?? error) }
    }
    // installBundle 的返回形态取决于 manager 是怎么拿到的：
    //   同进程 `ctx.inject(["pluginManager"])` → 直接就是 install 结果对象，
    //     带 `application` / `changed` / `version`，**没有 `ok` 字段**。
    //   跨进程 remote 代理（官方插件页走这条）→ 外面再包一层
    //     `{ ok: true, value }` 或 `{ ok: false, error }`。
    // 只按后者判断，会让同进程调用把每次成功都报成 "install failed"（实测踩过）。
    const wrapped = typeof answer?.ok === "boolean"
    if (wrapped && !answer.ok) return { ok: false, error: answer?.error?.message ?? "install failed" }
    const result = wrapped ? answer.value : answer
    if (result === null || result === undefined) {
      return { ok: false, error: "the plugin manager returned no result" }
    }
    if (result.application === "failed" || result.application === "cancelled") {
      return { ok: false, error: result.error?.diagnostic ?? result.error?.code ?? String(result.application) }
    }
    return {
      ok: true,
      state: "installed",
      tag,
      application: result.application,
      restartReason: result.restartReason,
      version: result.version,
      // 新实例起来后自己会把 Python 侧补齐；这里只说明它会发生。
      note: "Python 侧会在新实例启动时自动同步到同一版本",
    }
  }

  // ── Agent 工具 ───────────────────────────────────────────────────────────

  ctx.tools.register(
    defineTool({
      name: "ui_locate_update",
      description:
        "检查或安装 ui-locate 插件的新版本。action=check 只读 GitHub 的发布标签并报告版本差异；action=apply 会让 DSH 的插件管理器安装最新标签，并在新实例启动时同步 venv 里的 Python 包。apply 会改动本机已安装的插件。",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          action: {
            type: "string",
            description: "check（只检查，默认）或 apply（安装最新版本）",
          },
        },
      },
      output: {
        // 标准 JSON Schema：必填写在对象层的 required 数组里，不是属性层。
        // 每个 object 节点都要显式 additionalProperties，否则宿主编译器
        // 拒掉整份定义（见 README「必须知道的坑」第 16 条）。
        schema: {
          type: "object",
          additionalProperties: true,
          properties: {
            version: { type: "string" },
            repository: { type: "string" },
            home: { type: "string" },
            update: { type: "object", additionalProperties: true },
            python: { type: "object", additionalProperties: true },
            result: { type: "object", additionalProperties: true },
          },
          required: ["version", "repository", "home", "update", "python"],
        },
        render: (_args, value) => [{ type: "text", text: JSON.stringify(value, null, 2) }],
      },
      async execute(args) {
        const action = String(args?.action ?? "check")
        if (action !== "check" && action !== "apply") {
          return lossless({
            ...(await statusDocument()),
            result: { ok: false, error: `unknown action: ${action}` },
          })
        }
        if (action === "check") {
          if (current?.updateCheck !== false) await tracker.check()
          return lossless({ ...(await statusDocument()), result: { ok: true, action } })
        }
        const installed = await installLatest()
        return lossless({
          ...(await statusDocument()),
          result: { ok: installed.ok !== false, action, ...installed },
        })
      },
      presentCall: (args) => ({ card: "generic", title: "ui_locate_update", rawInput: args }),
    }),
  )

  /**
   * 准备运行环境。装上插件就能调，不需要用户先开终端。
   *
   * 之所以做成工具而不只是界面上的按钮：Agent 自己也该能修好自己的环境。
   * 首次调用要下 200MB 左右，所以超时给足。
   */
  ctx.tools.register(
    defineTool({
      name: "ui_locate_setup",
      description:
        "准备 ui-locate 的 Python 运行环境：建 venv、装依赖、下检测模型。首次约需下载 130MB 依赖与 77MB 模型。已就绪时直接返回现状；force=true 会删掉重装。会往磁盘写文件并启动 pip 子进程。",
      timeoutMs: 1_800_000,
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          force: { type: "boolean", description: "删掉现有的 venv 与模型重新装一遍，默认 false" },
          home: { type: "string", description: "这次使用的运行目录，留空用配置里的值" },
          modelDir: { type: "string", description: "这次使用的模型目录，留空用配置里的值" },
          backend: { type: "string", description: "directml / cuda / cpu，留空用配置里的值" },
        },
      },
      output: {
        schema: {
          type: "object",
          additionalProperties: true,
          properties: {
            ok: { type: "boolean" },
            home: { type: "string" },
            modelDir: { type: "string" },
            backend: { type: "string" },
            steps: { type: "array", items: { type: "object", additionalProperties: true } },
            error: { type: "string" },
            runtime: { type: "object", additionalProperties: true },
          },
          required: ["ok", "home", "modelDir", "backend"],
        },
        render: (_args, value) => [{ type: "text", text: JSON.stringify(value, null, 2) }],
      },
      async execute(args) {
        const target = plan({
          home: typeof args?.home === "string" ? args.home : undefined,
          modelDir: typeof args?.modelDir === "string" ? args.modelDir : undefined,
          backend: typeof args?.backend === "string" ? args.backend : undefined,
        })
        const result = await prepare(target, { force: args?.force === true })
        return lossless({
          ok: result.ok === true,
          home: target.home,
          modelDir: target.modelDir,
          backend: target.backend,
          steps: result.steps ?? [],
          error: result.error,
          runtime: await inspect(target),
        })
      },
      presentCall: (args) => ({ card: "generic", title: "ui_locate_setup", rawInput: args }),
    }),
  )

  // ── 设置页接口 ────────────────────────────────────────────────────────────

  const routing = createApi({
    plan: () => plan(),
    inspect: (target) => inspect(target),
    setup: (target, { force, onProgress }) => prepare(target, { force, onProgress }),
    logger: { warn: (line) => log(line) },
  })

  /**
   * 把四条路由挂到 webServer 上。
   *
   * 前缀 `/api/ui-locate` 比内核自己的 `/api` 长，而 webServer 是「最长前缀
   * 优先」，所以这些请求**绕过了内核的准入检查**——这就是为什么 `api.mjs`
   * 里第一件事是自己过一遍信任栅栏，`connection` 服务每次请求现取。
   *
   * `webServer` 可能比本插件晚挂载，所以走 `ctx.inject` 而不是在 apply 时
   * 读一次；读一次会永远拿到 `undefined`。
   */
  ctx.inject(["webServer"], (scoped) => {
    server = scoped.webServer
    scoped.effect(
      () =>
        server.register({
          kind: "prefix",
          path: "/api/ui-locate",
          handler: (req, res) => routing(req, res, readConnection(scoped)),
        }),
      "ui-locate: settings routes",
    )
    scoped.effect(
      () => () => {
        server = undefined
      },
      "ui-locate: web server",
    )
  })

  /** 每次请求现取 `connection`：它同样是晚挂载的，缓存会缓存到 undefined。 */
  function readConnection(scoped) {
    try {
      return scoped.get("connection")
    } catch {
      return undefined
    }
  }

  // ── 挂载与生命周期 ────────────────────────────────────────────────────────

  // 插件管理器按需注入：没有它，检查照常，只是装不了。
  ctx.inject(["pluginManager"], (child) => {
    manager = child.pluginManager
    child.effect(
      () => () => {
        manager = undefined
      },
      "ui-locate: plugin manager",
    )
  })

  // 配置变了就跟着变：开关关掉要真的停掉 timer，不能只是停止排期。
  ctx.effect(
    () =>
      ctx.on?.("loader/volatile-update", () => {
        readConfig()
        tracker.schedule(current?.updateCheck !== false)
        syncPython().catch((error) => {
          log(`python sync 失败: ${error?.message ?? error}`)
        })
      }),
    "ui-locate: follow the update settings",
  )

  readConfig()
  tracker.schedule(current?.updateCheck !== false)
  // Python 侧自愈：这一代实例启动时，如果 node_modules 里的新版本和 venv
  // 里装的不一致，就在这里补齐。首次安装时 venv 还不存在，会返回 missing。
  //
  // `void` 只是声明「不等它」，**不是错误处理**。syncPython 内部已经把失败
  // 折成 state:"failed"，这里再挂一个 catch 是第二道保险 —— 万一哪天有人改
  // 动了那个 then 的第二个参数，也不至于让一次同步失败炸掉插件加载。
  syncPython().catch((error) => {
    log(`python sync 失败: ${error?.message ?? error}`)
  })

  ctx.effect(
    () => () => {
      tracker.dispose()
    },
    "ui-locate: update timers",
  )
}

export { ToolArgsError }
