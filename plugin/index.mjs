/**
 * ui-locate 的 Host 侧插件：准备 Python 运行环境，并把它跟着 npm 包版本走。
 *
 * ## 为什么 ui-locate 需要一个 Host 插件
 *
 * 这个 bundle 的主体是一个 MCP 客户端行（`cordis.patch.yml` 的第一行），
 * 它只声明「用什么命令起 Python 进程」，本身没有可执行代码。DSH 的插件
 * 管理器能装 JS/YAML 那部分，但**装不到 venv 里的 Python 包**——而那才是
 * 真正会改进的部分（算法、模型、OCR 参数）。所以需要一段 Host 代码同时
 * 管住两边。
 *
 * ## 两件事
 *
 * 1. **准备运行环境**（`runtime.mjs`）：venv、依赖、模型。用 DSH 自带的
 *    Python，不要求用户先装 uv，也不需要终端。
 * 2. **界面**（`api.mjs` + `client.js`）：设置里一页「运行环境」，能看见
 *    装到哪一步、装在哪，并且能一键重来。
 *
 * ## 更新交给 DSH，这里只管 Python 侧
 *
 * 插件的新版本由 **DSH 插件页自己管**：依赖写成 registry 形式之后，那一页
 * 就会给这个包显示更新按钮，走宿主自己的 `pluginManager`。我们**不再**自己
 * 读版本列表、也不再自己装——两套并行的更新链路只会互相打架：各自认为什么
 * 是最新，而且我们那条装完之后还会把依赖写法改回 `github:`，宿主那边好不容易
 * 出现的按钮又没了。
 *
 * 剩下必须自己做的只有一件：npm 包换成新版之后，venv 里那份 Python 包还是
 * 旧的，这一层宿主不管，由启动时的同步补上。
 *
 * 同步之所以放**启动时**而不是装的时候：新版本的源码要等换上来之后才出现在
 * node_modules 里，在旧实例里装会拿旧路径去装新版本。放启动时也自带容错 ——
 * 这一次失败不影响 JS 侧已经装好，下次启动会重新发现不一致并再试一次。
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
import { stateDir, syncPythonToBundle } from "./core.mjs"
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
 * `tools` 必给：本插件要注册一个工具。
 *
 * `webServer` **刻意不写进 inject**：它是可选能力，写进去会让整个插件在
 * 没有它的部署里直接起不来，连环境都准备不了。改为在 apply 里用
 * `ctx.inject([...])` 按需拿。
 */
export const inject = ["tools"]

/** 包根目录：`plugin/` 的上一层，也就是被装进 node_modules 的那个目录。 */
const PACKAGE_ROOT = fileURLToPath(new URL("..", import.meta.url))

export const Config = z.object({
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
  /** webServer，拿到之后才有设置页接口；没有就只留工具。 */
  let server

  function log(line) {
    try {
      ctx.logger?.info?.("ui-locate: %s", line)
    } catch {
      /* 记日志失败不该影响功能 */
    }
  }

  /** 一次 `.get()` 读一个字段；缺省字段用 schema 默认值。 */
  function readConfig() {
    const parsed = config ?? Config({})
    const values = {}
    for (const field of ["pythonSync", "runtimeDir", "modelDir", "backend"]) {
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

  // ── Agent 工具 ───────────────────────────────────────────────────────────

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

  // 配置变了就跟着变：开关关掉就不该再动 venv。
  ctx.effect(
    () =>
      ctx.on?.("loader/volatile-update", () => {
        readConfig()
        syncPython().catch((error) => {
          log(`python sync 失败: ${error?.message ?? error}`)
        })
      }),
    "ui-locate: follow the runtime settings",
  )

  readConfig()
  // Python 侧自愈：这一代实例启动时，如果 node_modules 里的新版本和 venv
  // 里装的不一致，就在这里补齐。首次安装时 venv 还不存在，会返回 missing。
  //
  // `void` 只是声明「不等它」，**不是错误处理**。syncPython 内部已经把失败
  // 折成 state:"failed"，这里再挂一个 catch 是第二道保险 —— 万一哪天有人改
  // 动了那个 then 的第二个参数，也不至于让一次同步失败炸掉插件加载。
  syncPython().catch((error) => {
    log(`python sync 失败: ${error?.message ?? error}`)
  })
}

export { ToolArgsError }
