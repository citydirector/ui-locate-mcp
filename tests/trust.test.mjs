/**
 * 信任栅栏与默认目录的测试。
 *
 * 跑法：`node --test tests/`（包根目录下）。
 *
 * ## 为什么要在**本地**复刻一份 rc.3 的 admit
 *
 * 被测的是「插件怎么跟内核说话」，不是「内核怎么判」。真的内核不在这个仓库里
 * （它是 DSH 的包），而 mock 掉整个 `admit` 只会测出「我按我想的调了」——
 * 那正是这次踩的坑：旧代码把裸 `IncomingMessage` 递过去，形状上「差不多」，
 * 却没有一处测试能发现内核读 headers 的两种姿势不一样。
 *
 * 所以这里按 rc.3 的**已读契约**复刻三段语义（`api-request-trust.ts:121`
 * `isTrustedApiRequest`、`rpc-host.ts:117` `admit`、`authentication.ts:41`
 * `authenticate` 的 `failed → 503`），再用它当对手盘。复刻件只做契约里写着的事：
 *
 * - `headers` 允许 `Headers` 实例与普通对象，普通对象里数组值等于「没这个头」；
 * - `admit` 返回 `{peer}` 或 `{rejection: 401|403|503}`；
 * - 任一 verifier 抛错 → 503（不是 401，也不是 403）。
 *
 * 复刻件本身也测（「两种 headers 都能进」），因为它是后面所有断言的前提。
 *
 * ## 一条纪律
 *
 * 回退（admit 抛错 / 503）**不是放行**：凡是跨站、非 loopback、异源 Origin 的
 * 用例，在每一条路径上都必须拿到 403。这些断言比「放行」那几条更重要。
 */

import assert from "node:assert/strict"
import test from "node:test"

const { admissionRequest, rejectionFor } = await import("../plugin/trust.mjs")
const { defaultPaths, normalizeDefaultPath } = await import("../plugin/core.mjs")
const { runtimePlan } = await import("../plugin/runtime.mjs")

// ── rc.3 契约的本地复刻 ────────────────────────────────────────────────────

/** 与 `api-request-trust.ts:18` 同语义：Headers 走 get，普通对象按属性读。 */
function readHeader(headers, name) {
  if (headers instanceof Headers) return headers.get(name) ?? undefined
  const value = headers[name]
  return typeof value === "string" ? value : undefined
}

/** 与 `api-request-trust.ts:121` 同语义的三道栅栏（trustedHosts 在这里为空）。 */
function trustedByHost(request) {
  const host = readHeader(request.headers, "host")
  if (host === undefined) return false
  let hostUrl
  try {
    hostUrl = new URL(`http://${host}`)
  } catch {
    return false
  }
  if (!["127.0.0.1", "[::1]", "::1", "localhost"].includes(hostUrl.hostname.toLowerCase())) return false
  if (readHeader(request.headers, "sec-fetch-site") === "cross-site") return false
  const origin = readHeader(request.headers, "origin")
  if (origin === undefined) return true
  try {
    return new URL(origin).host === hostUrl.host
  } catch {
    return false
  }
}

/**
 * 与 `rpc-host.ts:117` + `authentication.ts:41` 同语义的 admit。
 *
 * @param options.verifier - 凭据验证器；返回 peer / undefined，或抛错。
 * @param options.received - 收集每次收到的 request，供断言形状。
 */
function rc3Connection({ verifier = () => undefined, received = [] } = {}) {
  return {
    admit(request) {
      received.push(request)
      if (!trustedByHost(request)) return { rejection: 403 }
      if (request.signal?.aborted || request.socket?.destroyed === true) return { rejection: 401 }
      let failed = false
      try {
        const peer = verifier(request)
        if (peer !== undefined) return { peer }
      } catch {
        failed = true
      }
      return { rejection: failed ? 503 : 401 }
    },
  }
}

/** loopback 同源、不带跨站标记的一个请求。 */
function loopback(extra = {}) {
  return { headers: { host: "127.0.0.1:7788", ...extra }, method: "POST", url: "/api/ui-locate/setup", socket: {} }
}

// ── 契约形状 ───────────────────────────────────────────────────────────────

test("admit 收到的是 Headers 实例，数组值按 append 合并", () => {
  const received = []
  const connection = rc3Connection({ received, verifier: () => ({ id: "peer" }) })
  const req = {
    headers: { host: "127.0.0.1:7788", "x-list": ["a", "b"], "x-single": "c" },
    method: "POST",
    url: "/api/ui-locate/setup",
    socket: { destroyed: false },
  }
  assert.equal(rejectionFor(req, connection), undefined)
  const facts = received[0]
  assert.ok(facts.headers instanceof Headers, "递出去的 headers 必须是 Headers 实例")
  assert.equal(facts.headers.get("host"), "127.0.0.1:7788")
  assert.equal(facts.headers.get("x-list"), "a, b")
  assert.equal(facts.headers.get("x-single"), "c")
  assert.equal(facts.method, "POST")
  assert.equal(facts.url, "/api/ui-locate/setup")
  assert.deepEqual(facts.socket, { destroyed: false })
})

test("admit 也接受普通对象形态的 headers（契约两种都允许）", () => {
  const connection = rc3Connection({ verifier: () => ({ id: "peer" }) })
  const facts = { headers: { host: "127.0.0.1:7788" }, socket: { destroyed: false } }
  assert.deepEqual(connection.admit(facts), { peer: { id: "peer" } })
  // 普通对象里数组值读不出来 —— 这就是裸 IncomingHttpHeaders 的风险所在。
  assert.equal(readHeader({ "x-list": ["a", "b"] }, "x-list"), undefined)
})

test("伪头与不可写头被跳过，其余头照旧送达", () => {
  const facts = admissionRequest({ headers: { ":path": "/x", host: "127.0.0.1:1", "x-ok": "1" }, socket: {} })
  const names = [...facts.headers.keys()]
  assert.equal(names.includes(":path"), false, "伪头不该进 Headers")
  assert.equal(facts.headers.get("host"), "127.0.0.1:1")
  assert.equal(facts.headers.get("x-ok"), "1")
})

test("socket 只呈现 {destroyed}，method/url 缺席时不编造", () => {
  assert.deepEqual(admissionRequest({ headers: {}, socket: { destroyed: true, remotePort: 1234 } }).socket, { destroyed: true })
  const bare = admissionRequest({ headers: {} })
  assert.deepEqual(bare.socket, { destroyed: false })
  assert.equal("method" in bare, false)
  assert.equal("url" in bare, false)
})

// ── admit 抛错 → 回退 ─────────────────────────────────────────────────────

test("admit 抛错 → 回退结构检查：loopback 同源请求放行", () => {
  const broken = {
    admit() {
      throw new TypeError("Cannot read properties of undefined (reading 'some')")
    },
  }
  assert.equal(rejectionFor(loopback(), broken), undefined)
  assert.equal(rejectionFor(loopback({ origin: "http://127.0.0.1:7788", "sec-fetch-site": "same-origin" }), broken), undefined)
})

test("admit 抛错 → 跨站 / 异源 / 非 loopback 仍然拒绝", () => {
  const broken = {
    admit() {
      throw new TypeError("boom")
    },
  }
  assert.equal(rejectionFor(loopback({ "sec-fetch-site": "cross-site" }), broken), 403)
  assert.equal(rejectionFor(loopback({ origin: "http://evil.example" }), broken), 403)
  assert.equal(rejectionFor({ headers: { host: "evil.example:7788" }, socket: {} }, broken), 403)
  assert.equal(rejectionFor({ headers: {}, socket: {} }, broken), 403)
})

// ── admit 返回 503 → 回退 ────────────────────────────────────────────────

test("admit 返回 503（验证器内部失败）→ 回退：loopback 同源放行", () => {
  const unavailable = { admit: () => ({ rejection: 503 }) }
  assert.equal(rejectionFor(loopback(), unavailable), undefined)
})

test("admit 返回 503 → 跨站 / 异源 / 非 loopback 仍然拒绝", () => {
  const unavailable = { admit: () => ({ rejection: 503 }) }
  assert.equal(rejectionFor(loopback({ "sec-fetch-site": "cross-site" }), unavailable), 403)
  assert.equal(rejectionFor(loopback({ origin: "http://evil.example" }), unavailable), 403)
  assert.equal(rejectionFor({ headers: { host: "evil.example:7788" }, socket: {} }, unavailable), 403)
})

test("admit 的 401 / 403 原样透传，不回退", () => {
  assert.equal(rejectionFor(loopback(), { admit: () => ({ rejection: 401 }) }), 401)
  assert.equal(rejectionFor(loopback(), { admit: () => ({ rejection: 403 }) }), 403)
})

test("admit 给出 {peer} 仍要过结构检查", () => {
  const trusting = { admit: () => ({ peer: { id: "p" } }) }
  assert.equal(rejectionFor(loopback(), trusting), undefined)
  assert.equal(rejectionFor(loopback({ "sec-fetch-site": "cross-site" }), trusting), 403)
})

// ── 无 connection：纯结构栅栏 ─────────────────────────────────────────────

test("没有 connection 时的结构栅栏", () => {
  assert.equal(rejectionFor(loopback()), undefined)
  assert.equal(rejectionFor({ headers: { host: "localhost:7788" }, socket: {} }), undefined)
  assert.equal(rejectionFor({ headers: { host: "[::1]:7788" }, socket: {} }), undefined)
  assert.equal(rejectionFor({ headers: { host: "192.168.1.10:7788" }, socket: {} }), 403)
  assert.equal(rejectionFor({ headers: { host: "evil.example" }, socket: {} }), 403)
  assert.equal(rejectionFor({ headers: { host: "127.0.0.1:7788", origin: "https://127.0.0.1:7788" }, socket: {} }), 403)
  assert.equal(rejectionFor({ headers: { host: "127.0.0.1:7788", origin: "null" }, socket: {} }), 403)
  assert.equal(rejectionFor({ headers: { host: "127.0.0.1:7788", "sec-fetch-site": "cross-site" }, socket: {} }), 403)
})

test("桌面转发留下的 dsh-app Referer 放行，但同时带浏览器标记就拒", () => {
  assert.equal(rejectionFor({ headers: { host: "127.0.0.1:7788", referer: "dsh-app://app" }, socket: {} }), undefined)
  assert.equal(
    rejectionFor({ headers: { host: "127.0.0.1:7788", referer: "dsh-app://app", origin: "dsh-app://app" }, socket: {} }),
    403,
  )
})

// ── 日志纪律 ──────────────────────────────────────────────────────────────

test("admit 失败只记错误消息（不含 cookie/authorization），同一错误只记一条", () => {
  const logs = []
  const logger = { warn: (line) => logs.push(line) }
  const connection = {
    admit() {
      throw new Error("provider exploded")
    },
  }
  const req = {
    headers: { host: "127.0.0.1:7788", cookie: "dsh-desktop-session=THE_SECRET", authorization: "Bearer TOPSECRET" },
    socket: {},
  }
  rejectionFor(req, connection, logger)
  rejectionFor(req, connection, logger)
  assert.equal(logs.length, 1, "同一错误应只记一次")
  assert.match(logs[0], /ui-locate: admission threw: provider exploded/)
  for (const line of logs) {
    assert.ok(!line.includes("THE_SECRET"), `日志里不该出现 cookie：${line}`)
    assert.ok(!line.includes("TOPSECRET"), `日志里不该出现 authorization：${line}`)
  }
})

test("503 也记一条（区别于抛错），且不带请求头", () => {
  const logs = []
  rejectionFor(loopback({ cookie: "k=SECRETCOOKIE" }), { admit: () => ({ rejection: 503 }) }, { warn: (line) => logs.push(line) })
  assert.equal(logs.length, 1)
  assert.match(logs[0], /ui-locate: admission unavailable/)
  assert.ok(!logs[0].includes("SECRETCOOKIE"))
})

// ── 默认目录 ──────────────────────────────────────────────────────────────

test("win32 默认目录统一为反斜杠，不再混用正斜杠", () => {
  const local = defaultPaths({ LOCALAPPDATA: "C:/Users/x/AppData/Local" }, "win32")
  assert.equal(local.home, "C:\\Users\\x\\AppData\\Local\\ui-locate")
  assert.equal(local.modelDir, "C:\\Users\\x\\AppData\\Local\\ui-locate\\models")
  assert.ok(!local.home.includes("/") && !local.modelDir.includes("/"))

  const portable = defaultPaths({ DSH_HOME: "D:/portable/data" }, "win32")
  assert.equal(portable.home, "D:\\portable\\data\\ui-locate")
  assert.equal(portable.modelDir, "D:\\portable\\data\\ui-locate\\models")
})

test("POSIX 默认目录不被反斜杠污染", () => {
  const posix = defaultPaths({ HOME: "/home/x" }, "linux")
  assert.equal(posix.home, "/home/x/.local/share/ui-locate")
  assert.equal(posix.modelDir, "/home/x/.local/share/ui-locate/models")
  assert.ok(!posix.home.includes("\\"))
})

test("空值与用户显式输入的路径都不被改写", () => {
  assert.equal(normalizeDefaultPath("", "win32"), "")
  const explicit = runtimePlan({
    home: "D:/my/models",
    modelDir: "D:/my/models2",
    defaultHome: "C:\\fallback",
    defaultModelDir: "C:\\fallback\\models",
  })
  assert.equal(explicit.home, "D:/my/models")
  assert.equal(explicit.modelDir, "D:/my/models2")
})
