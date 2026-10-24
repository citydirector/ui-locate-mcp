/**
 * 设置页背后的那组 HTTP 接口。
 *
 * 四个路由，全在 `/api/ui-locate/` 下：
 *
 * | 方法 | 路径 | 作用 |
 * |---|---|---|
 * | GET  | `/status` | 当前位置、装没装、缺什么，还有可选的默认值 |
 * | GET  | `/defaults` | 默认位置，用来填输入框的 placeholder |
 * | POST | `/setup` | 开始准备环境；`{ force }` 决定是否覆盖重装 |
 * | GET  | `/progress` | 轮询当前进度（长任务不能指望一次请求等到头） |
 *
 * 为什么进度要单独一条：装依赖 + 下模型是分钟级的，浏览器那边一路 fetch 等
 * 到底会被超时掐断，而服务端还在装。改成「POST 立刻返回 jobId，GET 轮询」，
 * 前端拿到的是持续推进的状态，而不是一个假失败。
 *
 * @module plugin/api.mjs
 */

import { rejectionFor } from "./trust.mjs"

/** 一次只允许跑一个安装任务。 */
let running = null

/** 进度快照。前端每次拿到整份，不需要自己累加。 */
function blankJob() {
  return { id: "", state: "idle", step: "", detail: "", startedAt: 0, finishedAt: 0, error: "" }
}

/**
 * 造一个接口处理器。
 *
 * @param options
 *   - `plan()`：返回当前生效的位置与默认值（每次现算，配置可能刚改）。
 *   - `inspect(plan)`：只读地看现状。
 *   - `setup(plan, { force, onProgress })`：真正去准备环境。
 *   - `logger`：可选日志。
 * @returns `(req, res, connection) => Promise<void>`
 */
export function createApi({ plan, inspect, setup, logger }) {
  let job = blankJob()

  function send(res, status, payload) {
    const body = JSON.stringify(payload)
    res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" })
    res.end(body)
  }

  async function readJson(req, limit = 64 << 10) {
    const chunks = []
    let size = 0
    for await (const chunk of req) {
      size += chunk.length
      if (size > limit) throw new Error("请求体过大")
      chunks.push(chunk)
    }
    const text = Buffer.concat(chunks).toString("utf8").trim()
    if (text === "") return {}
    return JSON.parse(text)
  }

  /**
   * 起一个安装任务。立刻返回，不等它跑完。
   *
   * 已经在跑时直接返回同一个 jobId：设置页可能被点两下，或者用户开了两个
   * 标签页，重复启动会两个进程同时往同一个 venv 里写。
   */
  function start(force, current) {
    // 已经在跑时把当前快照还回去，而不是再造一个任务：设置页可能被点两下，
    // 或者用户开了两个标签页，重复启动会两个 pip 进程同时往同一个 venv 里写。
    if (running !== null) return { started: false, job }
    const id = globalThis.crypto?.randomUUID?.() ?? `setup-${Date.now()}`
    job = { ...blankJob(), id, state: "running", step: "准备中", startedAt: Date.now() }
    const onProgress = (step, detail) => {
      job = { ...job, step, detail: detail ?? "" }
    }
    const promise = (async () => {
      try {
        const result = await setup(current, { force, onProgress })
        if (result.ok) {
          job = { ...job, state: "done", step: "完成", detail: "", finishedAt: Date.now() }
        } else {
          job = { ...job, state: "failed", error: result.error ?? "未知错误", finishedAt: Date.now() }
        }
        return result
      } catch (error) {
        job = { ...job, state: "failed", error: String(error?.message ?? error), finishedAt: Date.now() }
        return { ok: false, error: job.error }
      } finally {
        running = null
      }
    })()
    running = promise
    return { started: true, job }
  }

  return async function handle(req, res, connection) {
    const rejection = rejectionFor(req, connection)
    if (rejection !== undefined) {
      const reason = rejection === 401 ? "unauthorized" : rejection === 503 ? "admission unavailable" : "forbidden"
      return send(res, rejection, { error: reason })
    }

    const url = new URL(req.url ?? "/", "http://localhost")
    const route = url.pathname.replace(/^\/api\/ui-locate/, "").replace(/\/+$/, "") || "/"
    const method = String(req.method ?? "GET").toUpperCase()

    try {
      if (method === "GET" && route === "/defaults") {
        const current = plan()
        return send(res, 200, { home: current.defaultHome, modelDir: current.defaultModelDir })
      }

      if (method === "GET" && route === "/status") {
        const current = plan()
        const facts = await inspect(current)
        return send(res, 200, { ...facts, defaults: { home: current.defaultHome, modelDir: current.defaultModelDir }, job })
      }

      if (method === "GET" && route === "/progress") {
        return send(res, 200, job)
      }

      if (method === "POST" && route === "/setup") {
        const body = await readJson(req)
        const current = plan()
        // 落盘的位置以这次请求为准，不等配置刷新——用户可能刚改完输入框就点。
        if (typeof body.home === "string" && body.home.trim() !== "") current.home = body.home.trim()
        if (typeof body.modelDir === "string" && body.modelDir.trim() !== "") current.modelDir = body.modelDir.trim()
        if (typeof body.backend === "string") current.backend = body.backend
        const started = start(body.force === true, current)
        return send(res, 200, { started: started.started, job: started.job })
      }

      return send(res, 404, { error: `未知路由 ${method} ${route}` })
    } catch (error) {
      logger?.warn?.(`ui-locate: setting route failed: ${String(error?.message ?? error)}`)
      return send(res, 500, { error: String(error?.message ?? error) })
    }
  }
}
