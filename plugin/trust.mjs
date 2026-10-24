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
 * ## 两层，按序尝试
 *
 * 1. 组合里有 `connection` 服务时，直接问它要判定结果——这就是内核给自身
 *    `/api` 用的那套（信任栅栏 + 浏览器认证 cookie），插件不会比应用更松。
 * 2. 没有这个服务时，用一份结构等价的复刻：只认 loopback 的 Host、拒绝跨站
 *    fetch、客户端带了 Origin/Referer 就必须与 Host 同源。
 *
 * 判定失败一律拒（`503`），绝不因为「服务报错」就放行——那是把认证关掉。
 *
 * @module plugin/trust.mjs
 */

/** 同机调用方可能合法使用的地址。 */
const LOOPBACK_NAMES = new Set(["127.0.0.1", "[::1]", "::1", "localhost"])

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
 * @returns 拒绝原因，或 `undefined` 表示放行。原因串只用固定文案，不回显调用方输入。
 */
function structuralReason(req) {
  const headers = req?.headers ?? {}
  const host = authorityOf(headers.host, "http")
  if (host === null || !LOOPBACK_NAMES.has(host.hostname)) return "host-not-loopback"
  if (String(headers["sec-fetch-site"] ?? "").toLowerCase() === "cross-site") return "cross-site"
  for (const header of ["origin", "referer"]) {
    const raw = headers[header]
    if (typeof raw !== "string" || raw.trim() === "") continue
    if (header === "referer" && isDesktopRelayReferer(raw)) {
      // 桌面转发会剥掉两枚浏览器标记但留下这个 Referer；它不是 Origin 授权，
      // 也绝不覆盖已挂载的 Host 认证判定。
      if (headers.origin !== undefined || headers["sec-fetch-site"] !== undefined) return "desktop-relay-markers"
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
 * 判定一个请求该不该放行。
 *
 * @param req - `node:http` 的 IncomingMessage。
 * @param connection - 组合里的 `connection` 服务；没有则走复刻栅栏。
 * @returns `undefined` 表示放行，否则是要回的 HTTP 状态码。
 */
export function rejectionFor(req, connection) {
  // 服务存在时的两种拼法：新版是 `admit` 返回 `{rejection}`，0.1.7 之前是
  // `requestRejection` 直接返回状态码。读错一次就会把每个请求都判成 503。
  const admit = connection?.admit
  if (typeof admit === "function") {
    try {
      const admission = admit.call(connection, req)
      const status = admission && typeof admission === "object" ? admission.rejection : undefined
      if (status === undefined) return structuralReason(req) === undefined ? undefined : 403
      return status
    } catch {
      return 503
    }
  }
  const reject = connection?.requestRejection
  if (typeof reject === "function") {
    try {
      const status = reject.call(connection, req)
      if (status === undefined) return structuralReason(req) === undefined ? undefined : 403
      return status
    } catch {
      return 503
    }
  }
  return structuralReason(req) === undefined ? undefined : 403
}
