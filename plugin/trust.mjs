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
 * 1. 组合里有 `connection.admit` 时，把请求**规范成 rc.3 的
 *    `ConnectionTrustRequest`** 再交给它——这就是内核给自身 `/api` 用的那套
 *    （信任栅栏 + 浏览器认证 cookie），插件不会比应用更松。
 * 2. `admit` 抛错、或返回 `503` 时，**回退**到结构等价的复刻检查，而不是
 *    无条件拒绝。503 在 rc.3 里是「验证器内部失败」——它说的是内核自己没验
 *    成，不是这个请求不可信；照单全收会把「内核某个 provider 抛了」变成
 *    「设置页全红」，用户连重装环境都点不了。
 * 3. 没有 `admit`（0.1.x 的 `requestRejection` 同理）时，直接用复刻检查。
 *
 * 回退**不是放行**：复刻检查只认 loopback 的 Host、拒绝跨站 fetch、客户端
 * 带了 Origin/Referer 就必须与 Host 同源。跨站与非 loopback 在每一条路径上
 * 都过不去，包括 `admit` 抛错那条。
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
 * @param req - `node:http` 的 IncomingMessage。
 * @param connection - 组合里的 `connection` 服务；没有则走复刻栅栏。
 * @param logger - 可选日志器，`admit` 失败时记一条（只记消息，不记头）。
 * @returns `undefined` 表示放行，否则是要回的 HTTP 状态码。
 */
export function rejectionFor(req, connection, logger) {
  // 服务存在时的两种拼法：新版是 `admit` 返回 `{peer}` / `{rejection}`，0.1.7
  // 之前是 `requestRejection` 直接返回状态码。读错一次就会把每个请求都判成 503。
  const admit = connection?.admit
  if (typeof admit === "function") {
    let admission
    try {
      admission = admit.call(connection, admissionRequest(req))
    } catch (error) {
      // 调用形状不对、或验证器内部炸了。内核没给结论，就自己给一个结构结论。
      reportFailure(logger, "threw", error)
      return structuralRejection(req)
    }
    // `{peer}`：内核认可了这个请求（它已经过了 Host / 跨站 / Origin 三栅栏）。
    // 仍然过一遍结构检查——内核认了不等于插件这边也认，多一道不亏。
    if (admission !== null && typeof admission === "object" && "peer" in admission) {
      return structuralRejection(req)
    }
    const status = admission !== null && typeof admission === "object" ? admission.rejection : undefined
    if (status === undefined) return structuralRejection(req)
    if (status === 503) {
      // rc.3：503 = 有 provider 抛错导致「没验成」，不是「这个请求不可信」。
      reportFailure(logger, "unavailable")
      return structuralRejection(req)
    }
    return status
  }
  const reject = connection?.requestRejection
  if (typeof reject === "function") {
    // 0.1.x 的老拼法：这条分支保持原样（抛错就 503），只补一条日志。
    // `admit` 回退是因为 rc.3 的 503 有明确语义（验证器内部失败）；老接口
    // 没有这个区分，不该顺手改掉一个没人报过问题的分支。
    try {
      const status = reject.call(connection, req)
      if (status === undefined) return structuralRejection(req)
      return status
    } catch (error) {
      reportFailure(logger, "threw (requestRejection)", error)
      return 503
    }
  }
  return structuralRejection(req)
}
