/**
 * ui-locate 的 Host 侧插件：查新版本、装新版本、把 Python 侧同步过去。
 *
 * ## 为什么 ui-locate 需要一个 Host 插件
 *
 * 这个 bundle 的主体是一个 MCP 客户端行（`cordis.patch.yml` 的第一行），
 * 它只声明「用什么命令起 Python 进程」，本身没有可执行代码。DSH 的插件
 * 管理器能装 JS/YAML 那部分，但**装不到 venv 里的 Python 包**——而那才是
 * 真正会改进的部分（算法、模型、OCR 参数）。所以需要一段 Host 代码同时
 * 管住两边。
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
 * 的 tag 列表并记下结论。安装必须由一次显式的工具调用触发。
 *
 * 具体逻辑都在 `core.mjs`：那边一行 Cordis 都没有，可以直接用 node 测。
 */

import { existsSync } from "node:fs"
import z from "@deepseek-ai/schemastery"
import { defineTool } from "./tool.mjs"
import {
  UPDATE_REPOSITORY,
  bundledPythonVersion,
  compareVersions,
  createUpdateTracker,
  fetchReleaseTags,
  installedPythonVersion,
  packageVersion,
  stateDir,
  syncPythonToBundle,
  venvPython,
} from "./core.mjs"

export const name = "ui-locate-update"

/**
 * `tools` 必给：本插件要注册 `ui_locate_update`。
 *
 * `pluginManager` **刻意不写进 inject**：它是可选能力，写进去会让整个插件
 * 在没有插件管理器的部署里直接起不来，连「查版本」都做不了。改为在 apply
 * 里用 `ctx.inject([...])` 按需拿。
 */
export const inject = ["tools"]

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
})

/**
 * 挂载更新检查，并注册一个让 Agent 能显式触发安装的工具。
 *
 * @param ctx - 插件上下文（`tools` 已注入；`pluginManager` 可选）。
 * @param config - schema 解析后的 Config：每个字段一个 volatile 引用。
 */
export function apply(ctx, config) {
  /** 最近一次读到的配置值。 */
  let current
  /** 插件管理器，拿到之后才能安装；没有就只报告。 */
  let manager

  const tracker = createUpdateTracker({
    // 测试与自建镜像都可以改这个地址；默认打公开的 GitHub。
    fetchTags: ({ signal }) =>
      fetchReleaseTags({ api: process.env.DSH_UI_LOCATE_UPDATE_API || "https://api.github.com", signal }),
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
    for (const field of ["updateCheck", "pythonSync"]) {
      const ref = parsed[field]
      values[field] = ref !== undefined && typeof ref.get === "function" ? ref.get() : ref
    }
    current = values
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
    const home = stateDir()
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
    return syncPythonToBundle().then((result) => {
      if (result.state !== "current" && result.state !== "missing") log(`python sync: ${JSON.stringify(result)}`)
      return result
    })
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
    const spec = `git+https://github.com/${UPDATE_REPOSITORY}.git#${tag}`
    log(`installing ${spec}`)
    const requestId = globalThis.crypto?.randomUUID?.() ?? `ui-locate-${Date.now()}`
    let answer
    try {
      answer = await manager.installBundle(spec, { requestId })
    } catch (error) {
      return { ok: false, error: String(error?.message ?? error) }
    }
    if (!answer?.ok) return { ok: false, error: answer?.error?.message ?? "install failed" }
    const result = answer.value
    return {
      ok: true,
      state: "installed",
      tag,
      application: result?.application,
      restartReason: result?.restartReason,
      version: result?.version,
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
        void syncPython()
      }),
    "ui-locate: follow the update settings",
  )

  readConfig()
  tracker.schedule(current?.updateCheck !== false)
  // Python 侧自愈：这一代实例启动时，如果 node_modules 里的新版本和 venv
  // 里装的不一致，就在这里补齐。首次安装时 venv 还不存在，会返回 missing。
  void syncPython()

  ctx.effect(
    () => () => {
      tracker.dispose()
    },
    "ui-locate: update timers",
  )
}
