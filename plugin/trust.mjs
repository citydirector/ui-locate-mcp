/**
 * 设置页 HTTP 接口的信任栅栏。
 *
 * ## 为什么不能省
 *
 * 本插件注册的路径前缀是 `/api/ui-locate`，比内核自己的 `/api` 更长，而
 * webServer 的分发是「最长前缀优先」。也就是说**这些请求根本不会经过内核
 * 的准入检查**——不设防的话，任何能连到 loopback 的进程都能调它们，而这几个
 * 接口会真的往磁盘写东西、跑子进程装包。
 *
 * ## 三层，按序尝试
 *
 * 1. 组合里有 `connection.admit`（rc.3）时，把请求**规范成 rc.3 的
 *    `ConnectionTrustRequest`** 再交给它——这就是内核给自身 `/api` 用的那套
 *    （信任栅栏 + 浏览器认证 cookie），插件不会比应用更松。
 * 2. 只有旧宿主的 `requestRejection` 时，走那条版本分支。
 * 3. 两者都没有（0.1.x 之前）时，才用复刻的结构栅栏。
 *
 * ## 认证问不出结论就拒绝
 *
 * `admit` 抛错、返回未知形状、或返回 `503`（rc.3 里表示「验证器没验成」），
 * 一律回 **503**，**不退回结构栅栏**。Host / Origin / Fetch Metadata 回答的是
 * 「请求从哪来」，不是「谁发的」；用来源合规替代身份认证，等于把未认证调用
 * 送进装包、抓屏、点击这些接口。远程代理转发后上游看到的也可能是 loopback。
 *
 * 结构栅栏只在「宿主根本没有身份服务」时才作为旧版适配使用，并且跨站 /
 * 非 loopback 在每条路径上都过不去。
 *
 * ## 为什么必须规范化请求
 *
 * rc.3 的 `ConnectionTrustRequest` 是 `{headers, signal?, socket?, method?, url?}`，
 * `headers` 允许 `Headers` 实例或普通对象。裸 `IncomingMessage` 虽然在字段上
 * 勉强对得上（`headers` 是普通对象、`socket` 真实存在），但它的 headers 是
 * `IncomingHttpHeaders`：同名多值会变成**字符串数组**，而 rc.3 的两个消费点
 * （`isTrustedApiRequest`、cookie verifier）对数组一律读成「没有这个头」——
 * 于是 Host 判定失败、cookie 读不到，表现成一个莫名其妙的 403/503，而不是
 * 一个能查的错误。`socket` 也一样：契约里是 `{destroyed}` 这个最小读出形状，
 * 不是一整个 `net.Socket`。所以这里显式造一份契约形状，不赌对方只读几个字段。
 *
 * 日志纪律：失败只记错误消息。**绝不**打印 cookie / authorization 头。
 *
 * @module plugin/trust.mjs
 */

/** 同机调用方可能合法使用的地址。 */
const LOOPBACK_NAMES = new Set(["127.0.0.1", "[::1]", "::1", "localhost"])

/**
 * 读一个头，两种形状都支持（与 rc.3 `api-request-trust.ts` 的 `header()` 同语义）。
 *
 * @param headers - `Headers` 实例或普通对象。
 * @param name - 小写头名。
 * @returns 头值；不存在、或普通对象里存的是数组，都算没有。
 */
function headerValue(headers, name) {
  if (headers instanceof Headers) return headers.get(name) ?? undefined
  const value = headers?.[name]
  return typeof value === "string" ? value : undefined
}

/** 头是否存在（`desktop-relay-markers` 那条判定要区分「缺席」与「空串」）。 */
function hasHeader(headers, name) {
  if (headers instanceof Headers) return headers.has(name)
  return headers?.[name] !== undefined
}

/** 把 Host/Origin/Referer 拆成 `{scheme, hostname, port}`，端口按协议补默认值。 */
function authorityOf(value, defaultScheme) {
  if (typeof value !== "string" || value.trim() === "") return null
  let url
  try {
    url = new URL(value.includes("://") ? value.trim() : `${defaultScheme ?? "http"}://${value.trim()}`)
  } catch {
    return null
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null
  const port = url.port === "" ? (url.protocol === "https:" ? "443" : "80") : url.port
  return { scheme: url.protocol.replace(":", ""), hostname: url.hostname.toLowerCase(), port }
}

/** 桌面版把页面从 `dsh-app://app` 转发到 loopback 时会留下这个 Referer。 */
function isDesktopRelayReferer(value) {
  try {
    const url = new URL(value)
    return (
      url.protocol === "dsh-app:" &&
      url.hostname === "app" &&
      url.port === "" &&
      url.username === "" &&
      url.password === ""
    )
  } catch {
    return false
  }
}

/**
 * 复刻内核的 `isTrustedApiRequest`：Host 必须是 loopback（防 DNS rebinding）、
 * 不许跨站 fetch、Origin/Referer 若存在就必须与 Host 同源。
 *
 * @param req - `node:http` 的 IncomingMessage，或 `{headers}` 这样的形状。
 * @returns 拒绝原因，或 `undefined` 表示放行。原因串只用固定文案，不回显调用方输入。
 */
function structuralReason(req) {
  const headers = req?.headers ?? {}
  const host = authorityOf(headerValue(headers, "host"), "http")
  if (host === null || !LOOPBACK_NAMES.has(host.hostname)) return "host-not-loopback"
  if (String(headerValue(headers, "sec-fetch-site") ?? "").toLowerCase() === "cross-site") return "cross-site"
  for (const header of ["origin", "referer"]) {
    const raw = headerValue(headers, header)
    if (typeof raw !== "string" || raw.trim() === "") continue
    if (header === "referer" && isDesktopRelayReferer(raw)) {
      // 桌面转发会剥掉两枚浏览器标记但留下这个 Referer；它不是 Origin 授权，
      // 也绝不覆盖已挂载的 Host 认证判定。
      if (hasHeader(headers, "origin") || hasHeader(headers, "sec-fetch-site")) return "desktop-relay-markers"
      continue
    }
    const authority = authorityOf(raw.trim())
    if (authority === null) return `${header}-invalid`
    if (authority.scheme !== host.scheme || authority.hostname !== host.hostname || authority.port !== host.port) {
      return `${header}-mismatch`
    }
  }
  return undefined
}

/**
 * 把 `node:http` 的 `IncomingMessage` 规范成 rc.3 的 `ConnectionTrustRequest`。
 *
 * - `headers`：造一个 `Headers` 实例。`IncomingHttpHeaders` 的数组值逐个
 *   `append`，让「同名多值」在两种表示里语义一致（`Headers` 用逗号合并，
 *   与 HTTP/1.1 的语义相同）。
 * - `socket`：只给 `{destroyed}`，不把整只 `net.Socket` 递出去——契约只要这一个读出。
 * - `method` / `url`：原样带上，验证器可能要按方法与路由分流。
 *
 * 伪头（`:path`）不是合法 header 名，`Headers.append` 会直接抛，所以先跳过；
 * 单个头写不进去也只丢那一个头，不阻断整次判定（丢了 host 就轮不到放行）。
 *
 * @param req - `node:http` 的 IncomingMessage。
 * @returns 契约形状的请求对象。
 */
export function admissionRequest(req) {
  const headers = new Headers()
  for (const [name, value] of Object.entries(req?.headers ?? {})) {
    if (name.startsWith(":")) continue
    if (value === undefined || value === null) continue
    const items = Array.isArray(value) ? value : [value]
    for (const item of items) {
      try {
        headers.append(name, String(item))
      } catch {
        /* 非法头名/头值：丢掉这一个，其它头照旧 */
      }
    }
  }
  const request = { headers, socket: { destroyed: req?.socket?.destroyed === true } }
  if (typeof req?.method === "string" && req.method !== "") request.method = req.method
  if (typeof req?.url === "string" && req.url !== "") request.url = req.url
  return request
}

/** 结构检查的结论：放行是 `undefined`，拒绝是 `403`。 */
function structuralRejection(req) {
  return structuralReason(req) === undefined ? undefined : 403
}

/**
 * 同一条失败只报一次。
 *
 * 设置页的 `/progress` 是秒级轮询，`admit` 坏掉时每个请求都会失败——一条一条
 * 打就是刷屏，日志真正想看的另一件事会被埋掉。上限 32 条也够覆盖不同错误。
 */
const reported = new Set()

/**
 * 记录一次 `admit` 失败（**不含任何请求头内容**）。
 *
 * @param logger - 可选日志器（`{warn(line)}`）。
 * @param label - 失败种类。
 * @param error - 抛出来的东西；`503` 分支没有异常对象。
 */
function reportFailure(logger, label, error) {
  const detail = error === undefined ? "" : `: ${String(error?.message ?? error).slice(0, 200)}`
  const line = `ui-locate: admission ${label}${detail}`
  if (reported.has(line)) return
  // 没人收日志时不要占用额度：否则第一次失败发生在没有 logger 的调用里，
  // 后面真能记的那一次就被「已记过」挡住了。
  if (typeof logger?.warn !== "function") return
  if (reported.size >= 32) reported.clear()
  reported.add(line)
  try {
    logger.warn(line)
  } catch {
    /* 记日志失败不该影响判定 */
  }
}

/**
 * 判定一个请求该不该放行。
 *
 * 认证结论只有三种来源：内核 `admit`、旧宿主 `requestRejection`、以及两者都
 * 没有时的结构栅栏。前两者一旦问不出结论（抛错、未知形状、503），就按不可用
 * 拒绝，不退回结构栅栏。
 *
 * @param req - `node:http` 的 IncomingMessage。
 * @param connection - 组合里的 `connection` 服务；没有则走复刻栅栏。
 * @param logger - 可选日志器，失败时记一条（只记消息，不记头）。
 * @returns 放行是 `undefined`，否则是要回的 HTTP 状态码。
 */
export async function rejectionFor(req, connection, logger) {
  // 读取 `admit` 本身也要包在 try 里：新版 DSH 的 service proxy 会在插件没有
  // 声明 `connection.authentication` 依赖时拒绝读取它（内部要读 authentication），
  // 抛 `cannot get property "connection.authentication" without inject`。
  // 那不是「请求不可信」，但也不是「可以放行」——这一层拿不到身份结论，
  // 按不可用拒绝。
  let admit
  try {
    admit = connection?.admit
  } catch (error) {
    reportFailure(logger, "admit unavailable", error)
    return 503
  }

  if (typeof admit === "function") {
    let admission
    try {
      admission = await admit.call(connection, admissionRequest(req))
    } catch (error) {
      reportFailure(logger, "threw", error)
      return 503
    }
    if (admission === null || typeof admission !== "object") {
      reportFailure(logger, "unknown result")
      return 503
    }
    if ("rejection" in admission) {
      const status = admission.rejection
      if (status === 401 || status === 403) return status
      reportFailure(logger, "unavailable")
      return 503
    }
    const peer = admission.peer
    if (!peer || typeof peer !== "object") {
      reportFailure(logger, "unknown result")
      return 503
    }
    // 内核已经回答了身份；不再叠一层来源复刻判据，否则已认证的桌面 / 远程
    // 入口会被无依据地拒掉。撤销状态写在 peer.signal 上，有就检查。
    if (peer.signal?.aborted === true) return 401
    return undefined
  }

  let reject
  try {
    reject = connection?.requestRejection
  } catch (error) {
    reportFailure(logger, "requestRejection unavailable", error)
    return 503
  }
  if (typeof reject === "function") {
    // 0.1.x 的老拼法：这条分支保持原样（抛错就 503），只补一条日志。
    try {
      const status = await reject.call(connection, req)
      if (status === undefined) return structuralRejection(req)
      return status
    } catch (error) {
      reportFailure(logger, "threw (requestRejection)", error)
      return 503
    }
  }

  // 两个接口都没有：更老的宿主没有身份服务可问，只能按结构栅栏。
  return structuralRejection(req)
}
