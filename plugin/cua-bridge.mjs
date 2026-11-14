/**
 * CUA ↔ ui-locate 桥接（宿主侧）。
 *
 * ## 它解决什么
 *
 * ui-locate 有「在整张图里找文字，回像素坐标」的能力，但没有桌面权限；CUA 有「授权截图 / 在窗口里
 * 点一下」的能力，却看不见语义目标。两边都不该越界：所以这里只做三件事——
 *
 *   1. 通过 `ctx.cuaDispatch`（平台 CUA provider 发布的同一个 `cua` 执行函数）拿一张**当前授权窗口**的截图；
 *   2. 把这张图原样发给本机 loopback 上的 `/locate-image`，拿回 `image-pixels` 坐标的匹配；
 *   3. 需要动作时，用同一份截图坐标系发一次 click，然后再截一张图做**像素级变化证据**。
 *
 * ## 不做的事（都是刻意的）
 *
 * - 不 import `@deepseek-ai/dsh-tools`，也不自己起 NativeClient：授权、Agent 身份、取消、清理全在 CUA 那一侧。
 * - 不自己抓屏：没有 `cuaDispatch` 就明确失败，绝不改走 Python 的桌面动作（那是被 managed 模式拒绝的裸路径）。
 * - 不把「像素变了」说成「任务完成」：`dispatched` 与 `verified` 分开，后者只是指同一窗口的编码图前后不同。
 * - 不跟随重定向、不读 HTTP(S)_PROXY、只连 127.0.0.1/[::1]/localhost，并对响应体设硬上限与总时限。
 *
 * ## 与 Python 端的契约（v1）
 *
 * 请求：`{ image_b64, width, height, text, mode: 'locate' | 'detect' }`（只发这些键——服务端会拒绝未知字段）
 * 响应：`{ ok: true, contractVersion: 1, coordinateSpace: 'image-pixels', image: { width, height },
 *          matches: [{ text, confidence, box: { x, y, width, height }, center: { x, y } }] }`
 * 坐标是整张图片左上角起的像素，不归一化、不带屏幕 origin。
 *
 * @module ui-locate-mcp/cua-bridge
 */

import http from "node:http"

/** 宿主期望的分发面版本。 */
export const CUA_DISPATCH_VERSION = 1

/** 纯图片端点契约版本。 */
export const LOCATE_CONTRACT_VERSION = 1

/** 请求体上限：与 Python 端的 24MiB 对齐，超了本地就拒，不发出去。 */
export const MAX_REQUEST_BYTES = 24 * 1024 * 1024

/** 响应体上限：匹配结果是小 JSON，给 2MiB 已经很宽松。 */
export const MAX_RESPONSE_BYTES = 2 * 1024 * 1024

/** 一次桥接调用的默认总时限（毫秒）。 */
export const DEFAULT_TIMEOUT_MS = 30_000

/** 桥接自身的失败，`code` 供模型与日志分辨原因。 */
export class BridgeError extends Error {
  constructor(code, message, options) {
    super(message, options)
    this.name = "BridgeError"
    this.code = code
  }
}

/** 只允许本机回环地址；任何别的地址都是配置错误，不是可重试错误。 */
export function assertLoopback(baseUrl) {
  let url
  try {
    url = new URL(baseUrl)
  } catch (error) {
    throw new BridgeError("BAD_ENDPOINT", `ui-locate endpoint is not a URL: ${String(baseUrl)}`, { cause: error })
  }
  if (url.protocol !== "http:") {
    throw new BridgeError("BAD_ENDPOINT", `ui-locate endpoint must be plain http on loopback, got ${url.protocol}`)
  }
  const host = url.hostname
  const loopback = host === "127.0.0.1" || host === "::1" || host === "[::1]" || host === "localhost"
  if (!loopback) throw new BridgeError("BAD_ENDPOINT", `ui-locate endpoint must be loopback, got ${host}`)
  return url
}

function abortReason(signal) {
  return signal.reason ?? Object.assign(new Error("The operation was aborted"), { name: "AbortError" })
}

/**
 * 发一个 JSON 请求到 loopback。用 `node:http` 而不是 fetch：不经过任何环境代理、不跟随重定向、
 * 响应体按字节截断、总时限由我们自己掌握。
 * @param {object} options - 目标、方法、请求体、取消信号与上限。
 * @returns {Promise<unknown>} 解析后的 JSON。
 */
export async function requestJson(options) {
  const {
    baseUrl, path, method = "POST", body, signal,
    timeoutMs = DEFAULT_TIMEOUT_MS, maxResponseBytes = MAX_RESPONSE_BYTES, transport = http,
  } = options
  const url = assertLoopback(baseUrl)
  if (signal?.aborted) throw abortReason(signal)
  const payload = body === undefined ? undefined : Buffer.from(JSON.stringify(body), "utf8")
  if (payload !== undefined && payload.byteLength > MAX_REQUEST_BYTES) {
    throw new BridgeError("REQUEST_TOO_LARGE", `locate-image request is ${payload.byteLength} bytes; the service accepts at most ${MAX_REQUEST_BYTES}`)
  }
  const target = new URL(path, url)

  return await new Promise((resolve, reject) => {
    let settled = false
    const finish = (error, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (signal) signal.removeEventListener("abort", onAbort)
      if (error) reject(error)
      else resolve(value)
    }
    const timer = setTimeout(() => {
      const error = new BridgeError("TIMEOUT", `ui-locate ${method} ${target.pathname} exceeded ${timeoutMs} ms`)
      finish(error)
      request.destroy(error)
    }, timeoutMs)
    const onAbort = () => {
      const error = abortReason(signal)
      finish(error)
      request.destroy(error)
    }
    const request = transport.request({
      protocol: target.protocol,
      hostname: target.hostname,
      port: target.port,
      path: `${target.pathname}${target.search}`,
      method,
      agent: false,
      headers: {
        accept: "application/json",
        ...(payload === undefined ? {} : { "content-type": "application/json", "content-length": String(payload.byteLength) }),
      },
    }, (response) => {
      const status = response.statusCode ?? 0
      if (status >= 300 && status < 400) {
        response.resume()
        finish(new BridgeError("REDIRECT_REFUSED", `ui-locate ${method} ${target.pathname} answered ${status}; redirects are not followed`))
        request.destroy()
        return
      }
      const chunks = []
      let received = 0
      response.on("data", (chunk) => {
        received += chunk.byteLength
        if (received > maxResponseBytes) {
          const error = new BridgeError("RESPONSE_TOO_LARGE", `ui-locate ${target.pathname} answered more than ${maxResponseBytes} bytes`)
          finish(error)
          response.destroy(error)
          request.destroy(error)
          return
        }
        chunks.push(chunk)
      })
      response.on("error", (error) => finish(new BridgeError("TRANSPORT", `ui-locate response failed: ${error.message}`, { cause: error })))
      response.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8")
        let parsed
        try {
          parsed = text === "" ? undefined : JSON.parse(text)
        } catch (error) {
          finish(new BridgeError("BAD_JSON", `ui-locate ${target.pathname} did not return JSON (status ${status}): ${text.slice(0, 200)}`, { cause: error }))
          return
        }
        if (status < 200 || status >= 300) {
          const code = typeof parsed?.code === "string" ? parsed.code : "HTTP_STATUS"
          const detail = typeof parsed?.error === "string" ? parsed.error : text.slice(0, 200)
          finish(new BridgeError(code, `ui-locate ${target.pathname} answered ${status}: ${detail}`))
          return
        }
        finish(undefined, parsed)
      })
    })
    request.on("error", (error) => {
      if (settled) return
      finish(new BridgeError(error?.name === "AbortError" ? "ABORTED" : "TRANSPORT", `ui-locate ${method} ${target.pathname} failed: ${error.message}`, { cause: error }))
    })
    if (signal) signal.addEventListener("abort", onAbort, { once: true })
    if (payload !== undefined) request.write(payload)
    request.end()
  })
}

/**
 * 读取 `/health` 并确认它是托管且契约对齐的实例。
 * @param {object} options - baseUrl、取消信号、超时。
 * @returns {Promise<object>} 通过校验的 health 对象。
 */
export async function readManagedHealth(options) {
  const { baseUrl, signal, timeoutMs, transport } = options
  const health = await requestJson({ baseUrl, path: "/health", method: "GET", signal, timeoutMs: timeoutMs ?? 10_000, transport })
  const managed = health?.managed === true
  const version = health?.contractVersion
  if (version !== LOCATE_CONTRACT_VERSION) {
    throw new BridgeError("CONTRACT_MISMATCH",
      `ui-locate at ${baseUrl} does not speak contract v${LOCATE_CONTRACT_VERSION} (health.contractVersion=${JSON.stringify(version)}); `
      + "restart the service under the DSH bundle so it serves the contract version this plugin was built for")
  }
  if (!managed) {
    throw new BridgeError("CUA_MANAGED_REQUIRED",
      `ui-locate at ${baseUrl} is not in managed mode (health.managed=${JSON.stringify(health?.managed)}); `
      + "set UI_LOCATE_CUA_MANAGED=1 for the service and restart it, then retry. This bridge never falls back to the desktop routes")
  }
  if (health?.capabilities?.image !== true) {
    throw new BridgeError("CAPABILITY_MISSING", `ui-locate at ${baseUrl} does not report the image capability; refusing to send screenshots`)
  }
  if (health?.capabilities?.desktop !== false) {
    throw new BridgeError("CONTRACT_MISMATCH",
      `ui-locate at ${baseUrl} claims managed mode but still reports desktop routes enabled `
      + `(health.capabilities.desktop=${JSON.stringify(health?.capabilities?.desktop)}); the managed environment variable did not reach the service process — `
      + "restart it under the DSH bundle instead of reusing a standalone instance. This bridge never falls back to the desktop routes")
  }
  return health
}

/** 逐字段核对一条匹配；坐标必须落在整图内。 */
function readMatch(match, image) {
  const fail = (detail) => {
    throw new BridgeError("CONTRACT_MISMATCH", `locate-image match is malformed: ${detail}`)
  }
  if (match === null || typeof match !== "object") fail("match is not an object")
  if (typeof match.text !== "string") fail("text must be a string")
  if (!Number.isFinite(match.confidence) || match.confidence < 0 || match.confidence > 1) fail("confidence must be within 0..1")
  const box = match.box
  const center = match.center
  if (box === null || typeof box !== "object") fail("box is missing")
  for (const key of ["x", "y", "width", "height"]) {
    if (!Number.isFinite(box[key])) fail(`box.${key} must be a finite number`)
  }
  if (box.x < 0 || box.y < 0 || box.width <= 0 || box.height <= 0) fail("box must be a positive rectangle inside the image")
  if (box.x + box.width > image.width || box.y + box.height > image.height) fail("box leaves the image")
  if (center === null || typeof center !== "object") fail("center is missing")
  if (!Number.isFinite(center.x) || !Number.isFinite(center.y)) fail("center must be finite numbers")
  if (center.x < 0 || center.x > image.width || center.y < 0 || center.y > image.height) fail("center leaves the image")
  return {
    text: match.text,
    confidence: match.confidence,
    box: { x: box.x, y: box.y, width: box.width, height: box.height },
    center: { x: center.x, y: center.y },
  }
}

/**
 * 把 `/locate-image` 的响应校验成宿主可用的形状。
 * @param {unknown} payload - 解析后的 JSON。
 * @param {{width: number, height: number}} image - 我们发出去的那张图的尺寸。
 */
export function readLocateResponse(payload, image) {
  if (payload === null || typeof payload !== "object") throw new BridgeError("CONTRACT_MISMATCH", "locate-image response is not an object")
  if (payload.ok !== true) throw new BridgeError("CONTRACT_MISMATCH", `locate-image refused the request: ${JSON.stringify(payload)}`)
  if (payload.contractVersion !== LOCATE_CONTRACT_VERSION) {
    throw new BridgeError("CONTRACT_MISMATCH", `locate-image contractVersion=${JSON.stringify(payload.contractVersion)}, expected ${LOCATE_CONTRACT_VERSION}`)
  }
  if (payload.coordinateSpace !== "image-pixels") {
    throw new BridgeError("CONTRACT_MISMATCH", `locate-image coordinateSpace=${JSON.stringify(payload.coordinateSpace)}, expected image-pixels`)
  }
  if (payload.image?.width !== image.width || payload.image?.height !== image.height) {
    throw new BridgeError("CONTRACT_MISMATCH",
      `locate-image answered for ${payload.image?.width}x${payload.image?.height} but received ${image.width}x${image.height}`)
  }
  if (!Array.isArray(payload.matches)) throw new BridgeError("CONTRACT_MISMATCH", "locate-image matches must be an array")
  return { matches: payload.matches.map((match) => readMatch(match, image)) }
}

/**
 * 一次 locate 调用的完整客户端（健康检查 + 纯图片定位）。
 *
 * 支持**候选端点**：托管实例与用户遗留的 legacy 实例可能同时存在（旧服务占着
 * 7788 又杀不掉），所以这里按顺序试候选，只接受握手全过的那一个，并在本次调用
 * 内钉住它——不能让 health 打在 A 端口、locate 却发到 B 端口。
 */
export class LocateImageClient {
  constructor(options = {}) {
    const listed = options.baseUrls ?? (options.baseUrl === undefined ? ["http://127.0.0.1:7788"] : [options.baseUrl])
    this.baseUrls = listed.map((url) => new URL(String(url)).origin)
    // 提前校验一次：配置错了应该立刻报，而不是等到模型真去点的时候。
    for (const url of this.baseUrls) assertLoopback(url)
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS
    this.transport = options.transport
  }

  /**
   * 顺序试候选端点，返回**这一次调用**要用的 `{ baseUrl, health }`。
   *
   * 端点刻意不落在实例字段上：同一个 client 对象可能被两次并发调用共用，把
   * 「当前端点」写成可变字段，会让 A 调用的 locate 打到 B 调用选中的端口上。
   * 调用方拿到 baseUrl 之后必须显式传给 `locate` / `compare`，因此
   * 「health 与 locate 打在同一端口」由数据流保证，而不是靠对象状态。
   *
   * @param {AbortSignal} [signal] - 取消信号。
   */
  async resolveEndpoint(signal) {
    const failures = []
    for (const candidate of this.baseUrls) {
      try {
        return { baseUrl: candidate, health: await this.health(candidate, signal) }
      } catch (error) {
        if (signal?.aborted) throw error
        failures.push({ candidate, error })
      }
    }
    const informative = failures.find(({ error }) => error?.code !== "TRANSPORT" && error?.code !== "TIMEOUT")
    const chosen = informative ?? failures[0]
    const tried = failures.map(({ candidate, error }) => `${candidate}: ${error?.code ?? error?.message}`).join("; ")
    throw new BridgeError(chosen?.error?.code ?? "ENDPOINT_UNRESOLVED",
      `no usable ui-locate endpoint (${tried}); managed instances must serve contract v1 with managed=true — restart the service under the DSH bundle`,
      { cause: chosen?.error })
  }

  /** 确认某个端点上是托管且契约一致的实例。 */
  async health(baseUrl, signal) {
    if (typeof baseUrl !== "string" || baseUrl === "") throw new BridgeError("BAD_REQUEST", "health needs an explicit base URL")
    return await readManagedHealth({ baseUrl, signal, timeoutMs: this.timeoutMs, transport: this.transport })
  }

  /**
   * 在给定图片里找文字。
   * @param {object} options - baseUrl/imageB64/width/height/text/mode/signal。
   */
  async locate(options) {
    const { baseUrl, imageB64, width, height, text, mode = "locate", signal } = options
    if (typeof baseUrl !== "string" || baseUrl === "") {
      throw new BridgeError("BAD_REQUEST", "locate needs the endpoint this call resolved")
    }
    if (typeof imageB64 !== "string" || imageB64 === "") throw new BridgeError("BAD_REQUEST", "locate needs non-empty image data")
    if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
      throw new BridgeError("BAD_REQUEST", "locate needs positive integer image dimensions")
    }
    if (mode === "locate" && (typeof text !== "string" || text.trim() === "")) {
      throw new BridgeError("BAD_REQUEST", "locate mode needs a non-empty text")
    }
    const payload = await requestJson({
      baseUrl,
      path: "/locate-image",
      method: "POST",
      body: { image_b64: imageB64, width, height, text: mode === "detect" ? "" : text, mode },
      signal,
      timeoutMs: this.timeoutMs,
      transport: this.transport,
    })
    return readLocateResponse(payload, { width, height })
  }

  /**
   * 纯图片像素比较（`POST /compare-images`）：两张同一窗口的截图是否真的不同。
   *
   * 只有服务端在 `/health` 里声明 `capabilities.compare === true` 时才该调用它。
   * 每侧都要带 width/height：服务端用它们在**解码前**做尺寸与像素限额校验
   * （声明尺寸与图片头不一致会 400，而不是先分配内存再拒）。
   *
   * 尺寸不同的两张图不归这里判断——服务端会返回 `comparable: false` 与
   * `changed: null`，调用方据尺寸自己得出「尺寸变了」。所以这里只在尺寸相同
   * 时才被调用。
   */
  async compare(options) {
    const { baseUrl, beforeB64, afterB64, width, height, signal } = options
    if (typeof baseUrl !== "string" || baseUrl === "") throw new BridgeError("BAD_REQUEST", "compare needs the endpoint this call resolved")
    if (typeof beforeB64 !== "string" || beforeB64 === "" || typeof afterB64 !== "string" || afterB64 === "") {
      throw new BridgeError("BAD_REQUEST", "compare needs two non-empty images")
    }
    if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
      throw new BridgeError("BAD_REQUEST", "compare needs positive integer image dimensions")
    }
    const side = (imageB64) => ({ image_b64: imageB64, width, height })
    const payload = await requestJson({
      baseUrl,
      path: "/compare-images",
      method: "POST",
      body: { before: side(beforeB64), after: side(afterB64) },
      signal,
      timeoutMs: this.timeoutMs,
      transport: this.transport,
    })
    return readCompareResponse(payload, { width, height })
  }
}

/**
 * 校验 `/compare-images` 的响应。
 *
 * 只认 `changed` 布尔与图像尺寸：其它字段（差多少像素、比例）原样透传，缺失
 * 就是 `null`，不拿它编造置信度。
 * @param {unknown} payload - 解析后的 JSON。
 * @param {{width: number, height: number}} image - 两张图共同的尺寸。
 */
export function readCompareResponse(payload, image) {
  if (payload === null || typeof payload !== "object") throw new BridgeError("CONTRACT_MISMATCH", "compare-images response is not an object")
  if (payload.ok !== true) throw new BridgeError("CONTRACT_MISMATCH", `compare-images refused the request: ${JSON.stringify(payload)}`)
  if (payload.contractVersion !== LOCATE_CONTRACT_VERSION) {
    throw new BridgeError("CONTRACT_MISMATCH", `compare-images contractVersion=${JSON.stringify(payload.contractVersion)}, expected ${LOCATE_CONTRACT_VERSION}`)
  }
  if (typeof payload.comparable !== "boolean") throw new BridgeError("CONTRACT_MISMATCH", "compare-images comparable must be a boolean")
  if (payload.comparable) {
    if (typeof payload.changed !== "boolean") throw new BridgeError("CONTRACT_MISMATCH", "compare-images changed must be a boolean when comparable")
    if (payload.image?.width !== image.width || payload.image?.height !== image.height) {
      throw new BridgeError("CONTRACT_MISMATCH",
        `compare-images answered for ${payload.image?.width}x${payload.image?.height} but received ${image.width}x${image.height}`)
    }
  } else if (payload.changed !== null) {
    // 服务端把「不可比」明确写成 null；false 会被读成「比过了，没变」。
    throw new BridgeError("CONTRACT_MISMATCH", `compare-images changed must be null when comparable is false (got ${JSON.stringify(payload.changed)})`)
  }
  const finite = (value) => (Number.isFinite(value) ? value : null)
  return {
    comparable: payload.comparable,
    changed: payload.comparable ? payload.changed : null,
    changedPixels: finite(payload.changedPixels),
    totalPixels: finite(payload.totalPixels),
    changedRatio: finite(payload.changedRatio),
    maxChannelDelta: finite(payload.maxChannelDelta),
    changedBox: payload.changedBox ?? null,
    reason: typeof payload.reason === "string" ? payload.reason : null,
  }
}

/** 构造一次授权截图请求（与 `cua` 工具完全同形）。 */
export function screenshotRequest({ appHandle, windowId }) {
  return { method: "screenshot", params: { appHandle, windowId } }
}

/**
 * 构造一次坐标点击请求。坐标是**截图里的像素**，与 `coordinateSpace: 'screenshot-pixels'` 对应。
 * @param {object} options - 目标窗口、像素坐标、按钮与投递方式。
 */
export function clickRequest({ appHandle, windowId, x, y, mouseButton = "left", clickCount = 1, deliveryMode = "background" }) {
  return {
    method: "click",
    params: { appHandle, windowId, x, y, relative: false, mouseButton, clickCount, deliveryMode },
  }
}

/** 把截图结果收敛成 Host 需要的最小事实。 */
function readScreenshot(result) {
  const data = result?.data
  const width = result?.pixelWidth
  const height = result?.pixelHeight
  if (typeof data !== "string" || data === "") throw new BridgeError("SCREENSHOT_UNAVAILABLE", "cua screenshot returned no image data")
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    throw new BridgeError("SCREENSHOT_UNAVAILABLE", "cua screenshot returned no pixel dimensions")
  }
  return { data, width, height, windowId: result?.windowId }
}

/** 取当前发布的分发面；缺失或版本不符就在这里失败，绝不回落到 Python 桌面路由。 */
function requireDispatch(getDispatch) {
  const dispatch = getDispatch()
  if (dispatch === undefined || dispatch === null) {
    throw new BridgeError("CUA_DISPATCH_UNAVAILABLE",
      "no CUA provider is installed (ctx.cuaDispatch is missing); install the desktop computer-use provider. "
      + "This tool will not fall back to ui-locate desktop routes")
  }
  if (dispatch.version !== CUA_DISPATCH_VERSION) {
    throw new BridgeError("CUA_DISPATCH_VERSION", `ctx.cuaDispatch.version is ${JSON.stringify(dispatch.version)}, expected ${CUA_DISPATCH_VERSION}`)
  }
  if (typeof dispatch.execute !== "function") throw new BridgeError("CUA_DISPATCH_VERSION", "ctx.cuaDispatch.execute is not a function")
  return dispatch
}

function requireExecution(exec) {
  if (exec === undefined || exec === null || typeof exec !== "object") {
    throw new BridgeError("EXECUTION_REQUIRED", "this tool must run under an agent execution: it forwards that execution to the CUA provider")
  }
  if (typeof exec.agent !== "object" || exec.agent === null) {
    throw new BridgeError("EXECUTION_REQUIRED", "this tool must run under an agent-owned execution")
  }
  if (typeof exec.callId !== "string" || exec.callId === "") {
    throw new BridgeError("EXECUTION_REQUIRED", "this execution has no call id")
  }
  if (exec.signal === undefined || typeof exec.signal?.throwIfAborted !== "function") {
    throw new BridgeError("EXECUTION_REQUIRED", "this execution has no cancellation signal")
  }
  return exec
}

/** 在动作之间检查取消：撤销/停止之后不再发起截图、HTTP 或输入。 */
function ensureLive(exec) {
  exec.signal.throwIfAborted()
}

/**
 * 生成两个宿主工具所需的执行函数（不含工具定义，方便测试直接调用）。
 * @param {object} options - `{ getDispatch, client, logger }`。
 */
export function createBridgeOperations(options) {
  const { getDispatch, client, logger } = options
  if (typeof getDispatch !== "function") throw new TypeError("createBridgeOperations needs getDispatch()")
  if (!client) throw new TypeError("createBridgeOperations needs a LocateImageClient")

  /**
   * 截图 + 定位；不动作。
   *
   * 端点只解析一次：health 过了之后，截图与 locate 都用这一个 baseUrl，别让
   * 「握手在 7789、请求发到 7788」这种事发生。
   */
  async function locate(args, exec) {
    const execution = requireExecution(exec)
    const dispatch = requireDispatch(getDispatch)
    ensureLive(execution)
    const endpoint = await client.resolveEndpoint(execution.signal)
    ensureLive(execution)
    const shot = readScreenshot(await dispatch.execute(
      screenshotRequest({ appHandle: args.appHandle, windowId: args.windowId }), execution))
    ensureLive(execution)
    const found = await client.locate({
      baseUrl: endpoint.baseUrl,
      imageB64: shot.data, width: shot.width, height: shot.height, text: args.text, mode: "locate", signal: execution.signal,
    })
    ensureLive(execution)
    const selected = found.matches[0] ?? null
    logger?.debug?.(`cua-bridge locate: ${found.matches.length} match(es) in ${shot.width}x${shot.height}`)
    return {
      route: "cua-screenshot+locate-image",
      coordinateSpace: "screenshot-pixels",
      image: { width: shot.width, height: shot.height },
      windowId: shot.windowId,
      matches: found.matches,
      selected,
      dispatched: false,
      verification: "not-requested",
    }
  }

  /**
   * 一次像素级证据比较。
   *
   * 只认服务端 `changed` 布尔与图像尺寸：尺寸变了本身就是「变了」（那是调用方
   * 能自己判断的事实），尺寸一致才问服务端；问不到就返回 `verified: null`
   * （未知），绝不悄悄当成「没变化」。
   */
  async function compareEvidence({ endpoint, before, after, signal }) {
    const detail = {
      kind: "pixel-compare",
      before: { width: before.width, height: before.height },
      after: { width: after.width, height: after.height },
      beforeBytes: Buffer.from(before.data, "base64").byteLength,
      afterBytes: Buffer.from(after.data, "base64").byteLength,
      differingPixels: null,
      ratio: null,
    }
    if (before.width !== after.width || before.height !== after.height) {
      return { verified: true, verification: "dimension-changed", detail: { ...detail, changed: true, dimensionChanged: true } }
    }
    try {
      const compared = await client.compare({
        baseUrl: endpoint.baseUrl,
        beforeB64: before.data,
        afterB64: after.data,
        width: before.width,
        height: before.height,
        signal,
      })
      if (!compared.comparable) {
        return {
          verified: null,
          verification: "image-compare-unavailable",
          detail: { ...detail, changed: null, comparable: false, reason: compared.reason ?? "not-comparable" },
        }
      }
      return {
        verified: compared.changed === true,
        verification: compared.changed === true ? "pixels-changed" : "pixels-unchanged",
        detail: {
          ...detail,
          changed: compared.changed,
          comparable: true,
          changedPixels: compared.changedPixels,
          totalPixels: compared.totalPixels,
          changedRatio: compared.changedRatio,
          maxChannelDelta: compared.maxChannelDelta,
          changedBox: compared.changedBox,
        },
      }
    } catch (error) {
      if (signal?.aborted) throw error
      return {
        verified: null,
        verification: "image-compare-unavailable",
        detail: {
          ...detail,
          changed: null,
          error: { code: error?.code ?? "COMPARE_FAILED", message: String(error?.message ?? error) },
        },
      }
    }
  }

  /**
   * 定位 → 点击 → 再截图，用像素比较出证据。
   *
   * 全程只有**一份**观察：locate 用的就是这张截图，点的也是这张截图里的
   * center——中间不再截一次图把坐标账本刷新掉却仍用旧坐标。
   *
   * 点击前必须确认纯图片端点有 `capabilities.compare`：没有它就没有真实像素
   * 证据，此时**不点击**，而不是拿编码字节冒充验证。
   */
  async function act(args, exec) {
    const execution = requireExecution(exec)
    const dispatch = requireDispatch(getDispatch)
    ensureLive(execution)

    const endpoint = await client.resolveEndpoint(execution.signal)
    const canCompare = endpoint.health?.capabilities?.compare === true
    ensureLive(execution)

    const before = readScreenshot(await dispatch.execute(
      screenshotRequest({ appHandle: args.appHandle, windowId: args.windowId }), execution))
    ensureLive(execution)
    const found = await client.locate({
      baseUrl: endpoint.baseUrl,
      imageB64: before.data, width: before.width, height: before.height, text: args.text, mode: "locate", signal: execution.signal,
    })
    ensureLive(execution)
    const selected = found.matches[0] ?? null
    const looked = {
      route: "cua-screenshot+locate-image",
      coordinateSpace: "screenshot-pixels",
      image: { width: before.width, height: before.height },
      windowId: before.windowId,
      matches: found.matches,
      selected,
    }
    if (selected === null) {
      return { ...looked, dispatched: false, verification: "no-match", verified: false, note: "no match; nothing was dispatched" }
    }
    if (!canCompare) {
      return {
        ...looked,
        dispatched: false,
        verification: "image-compare-unavailable",
        verified: null,
        note: "the pure-image service does not report capabilities.compare; refusing to dispatch without a real pixel check",
      }
    }
    const delivery = args.deliveryMode === "foreground" ? "foreground" : "background"
    const clicked = await dispatch.execute(clickRequest({
      appHandle: args.appHandle,
      windowId: args.windowId,
      x: selected.center.x,
      y: selected.center.y,
      clickCount: args.clickCount === 2 ? 2 : 1,
      deliveryMode: delivery,
    }), execution)
    const dispatched = clicked?.dispatched === true
    ensureLive(execution)
    const after = readScreenshot(await dispatch.execute(
      screenshotRequest({ appHandle: args.appHandle, windowId: args.windowId }), execution))
    // 操作后仍必须是同一个窗口：换了窗口，前后两张图比什么都没意义。
    if (before.windowId !== undefined && after.windowId !== undefined && before.windowId !== after.windowId) {
      throw new BridgeError("WINDOW_CHANGED", "the screenshot after the click belongs to a different window; nothing about the evidence can be trusted")
    }
    const evidence = await compareEvidence({ endpoint, before, after, signal: execution.signal })
    return {
      ...looked,
      clicked: { x: selected.center.x, y: selected.center.y, deliveryMode: delivery },
      dispatched,
      verified: evidence.verified,
      verification: evidence.verification,
      evidence: evidence.detail,
      note: "verified only means the same window's pixels changed; it does not prove the task succeeded",
    }
  }

  return { locate, act }
}
