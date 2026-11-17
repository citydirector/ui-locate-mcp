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
 * 认证问不出结论就拒绝：`admit` 抛错、返回未知形状或 503 时一律 503，不退回
 * 结构栅栏。来源合规不等于身份可信——跨站 / 非 loopback / 异源 Origin 的断言
 * 仍然保留，但它们只在「宿主根本没有身份服务」的旧版分支里才会拿到 403。
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

test("admit 收到的是 Headers 实例，数组值按 append 合并", async () => {
  const received = []
  const connection = rc3Connection({ received, verifier: () => ({ id: "peer" }) })
  const req = {
    headers: { host: "127.0.0.1:7788", "x-list": ["a", "b"], "x-single": "c" },
    method: "POST",
    url: "/api/ui-locate/setup",
    socket: { destroyed: false },
  }
  assert.equal(await rejectionFor(req, connection), undefined)
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

// ── 认证问不出结论：一律 503，不退回结构栅栏 ──────────────────────────────

test("admit 抛错 → 503（loopback 同源也不放行）", async () => {
  const broken = {
    admit() {
      throw new TypeError("Cannot read properties of undefined (reading 'some')")
    },
  }
  assert.equal(await rejectionFor(loopback(), broken), 503)
  assert.equal(await rejectionFor(loopback({ origin: "http://127.0.0.1:7788", "sec-fetch-site": "same-origin" }), broken), 503)
})

test("admit 抛错 → 跨站 / 异源 / 非 loopback 同样是 503", async () => {
  const broken = {
    admit() {
      throw new TypeError("boom")
    },
  }
  assert.equal(await rejectionFor(loopback({ "sec-fetch-site": "cross-site" }), broken), 503)
  assert.equal(await rejectionFor(loopback({ origin: "http://evil.example" }), broken), 503)
  assert.equal(await rejectionFor({ headers: { host: "evil.example:7788" }, socket: {} }, broken), 503)
  assert.equal(await rejectionFor({ headers: {}, socket: {} }, broken), 503)
})

test("读 admit 就被 service proxy 拒绝（未声明 connection.authentication）→ 503", async () => {
  // 新版 DSH 的 cordis proxy 在插件没声明 `connection.authentication` 时，
  // 连属性读取都会抛。这一层拿不到身份结论，只能拒绝——来源合规不等于已认证。
  const guarded = {
    get admit() {
      throw new Error('cannot get property "connection.authentication" without inject')
    },
  }
  assert.equal(await rejectionFor(loopback(), guarded), 503)
  assert.equal(await rejectionFor(loopback({ "sec-fetch-site": "cross-site" }), guarded), 503)
  assert.equal(await rejectionFor({ headers: { host: "evil.example:7788" }, socket: {} }, guarded), 503)
})

test("读 requestRejection 被拒绝 → 503", async () => {
  const guarded = {
    get requestRejection() {
      throw new Error('cannot get property "connection.authentication" without inject')
    },
  }
  assert.equal(await rejectionFor(loopback(), guarded), 503)
})

// ── admit 的拒绝状态：401/403 透传，503 拒绝 ───────────────────────────────

test("admit 返回 503（验证器没验成）→ 503，不放行", async () => {
  const unavailable = { admit: () => ({ rejection: 503 }) }
  assert.equal(await rejectionFor(loopback(), unavailable), 503)
  assert.equal(await rejectionFor(loopback({ "sec-fetch-site": "cross-site" }), unavailable), 503)
})

test("admit 的 401 / 403 原样透传", async () => {
  assert.equal(await rejectionFor(loopback(), { admit: () => ({ rejection: 401 }) }), 401)
  assert.equal(await rejectionFor(loopback(), { admit: () => ({ rejection: 403 }) }), 403)
})

test("admit 返回未知形状 → 503", async () => {
  assert.equal(await rejectionFor(loopback(), { admit: () => null }), 503)
  assert.equal(await rejectionFor(loopback(), { admit: () => ({}) }), 503)
  assert.equal(await rejectionFor(loopback(), { admit: () => ({ peer: null }) }), 503)
  assert.equal(await rejectionFor(loopback(), { admit: () => ({ rejection: 418 }) }), 503)
})

test("admit 给出有效 peer → 直接放行，不再叠结构判据", async () => {
  const peer = { id: "p", signal: { aborted: false } }
  const trusting = { admit: () => ({ peer }) }
  assert.equal(await rejectionFor(loopback(), trusting), undefined)
  // 内核已经回答了身份；跨站 / 非 loopback 是内核那道栅栏的事，插件不重复判。
  assert.equal(await rejectionFor(loopback({ "sec-fetch-site": "cross-site" }), trusting), undefined)
  assert.equal(await rejectionFor({ headers: { host: "evil.example:7788" }, socket: {} }, trusting), undefined)
})

test("peer 已撤销 → 401", async () => {
  const revoked = { admit: () => ({ peer: { id: "p", signal: { aborted: true } } }) }
  assert.equal(await rejectionFor(loopback(), revoked), 401)
})

test("admit 是异步的也能用", async () => {
  const asyncAdmit = { admit: async () => ({ peer: { id: "p", signal: { aborted: false } } }) }
  assert.equal(await rejectionFor(loopback(), asyncAdmit), undefined)
})

// ── 无 connection：纯结构栅栏 ─────────────────────────────────────────────

test("没有 connection 时的结构栅栏（更老的宿主）", async () => {
  assert.equal(await rejectionFor(loopback()), undefined)
  assert.equal(await rejectionFor({ headers: { host: "localhost:7788" }, socket: {} }), undefined)
  assert.equal(await rejectionFor({ headers: { host: "[::1]:7788" }, socket: {} }), undefined)
  assert.equal(await rejectionFor({ headers: { host: "192.168.1.10:7788" }, socket: {} }), 403)
  assert.equal(await rejectionFor({ headers: { host: "evil.example" }, socket: {} }), 403)
  assert.equal(await rejectionFor({ headers: { host: "127.0.0.1:7788", origin: "https://127.0.0.1:7788" }, socket: {} }), 403)
  assert.equal(await rejectionFor({ headers: { host: "127.0.0.1:7788", origin: "null" }, socket: {} }), 403)
  assert.equal(await rejectionFor({ headers: { host: "127.0.0.1:7788", "sec-fetch-site": "cross-site" }, socket: {} }), 403)
})

test("桌面转发留下的 dsh-app Referer 放行，但同时带浏览器标记就拒", async () => {
  assert.equal(await rejectionFor({ headers: { host: "127.0.0.1:7788", referer: "dsh-app://app" }, socket: {} }), undefined)
  assert.equal(
    await rejectionFor({ headers: { host: "127.0.0.1:7788", referer: "dsh-app://app", origin: "dsh-app://app" }, socket: {} }),
    403,
  )
})

// ── 日志纪律 ──────────────────────────────────────────────────────────────

test("admit 失败只记错误消息（不含 cookie/authorization），同一错误只记一条", async () => {
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
  await rejectionFor(req, connection, logger)
  await rejectionFor(req, connection, logger)
  assert.equal(logs.length, 1, "同一错误应只记一次")
  assert.match(logs[0], /ui-locate: admission threw: provider exploded/)
  for (const line of logs) {
    assert.ok(!line.includes("THE_SECRET"), `日志里不该出现 cookie：${line}`)
    assert.ok(!line.includes("TOPSECRET"), `日志里不该出现 authorization：${line}`)
  }
})

test("503 也记一条（区别于抛错），且不带请求头", async () => {
  const logs = []
  await rejectionFor(loopback({ cookie: "k=SECRETCOOKIE" }), { admit: () => ({ rejection: 503 }) }, { warn: (line) => logs.push(line) })
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
