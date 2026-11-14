/**
 * CUA ↔ ui-locate 桥接的测试。
 *
 * 跑法：`node --test tests/`（包根目录下）。
 *
 * 三层：
 *  - `plugin/cua-bridge.mjs` 的纯判定：loopback 白名单、health 握手、响应形状。
 *    HTTP 用本机临时端口上的真 `node:http` 服务顶替——不出回环，也不 mock 掉
 *    自己写的客户端（那样测的只是 mock）。
 *  - 真实 HTTP 行为：重定向、响应过大、非 JSON、服务端错误码、超时、取消。
 *  - 桥接操作与 Host 接线：`dispatched` / `verified` 分开、无匹配不点击、
 *    不支持后台不升级、撤销（取消）之后不再发请求、缺 `cuaDispatch` 时绝不
 *    回落到 Python 桌面路由。
 *
 * 一条纪律：**原生不做假造**。这里所有 `cuaDispatch` 都是替身，本机没有
 * .NET SDK，helper 实际编译与真实窗口的前后台行为属于未验项（见报告），
 * 不在这里假装跑过。
 */

import assert from "node:assert/strict"
import http from "node:http"
import test from "node:test"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"

const ROOT = fileURLToPath(new URL("..", import.meta.url))

// index.mjs 依赖 Host 提供的 schemastery；桩与其它测试共用。
const { ensureStubs } = await import("./stubs.mjs")
ensureStubs(ROOT)

const {
  BridgeError,
  LocateImageClient,
  assertLoopback,
  clickRequest,
  createBridgeOperations,
  readCompareResponse,
  readLocateResponse,
  readManagedHealth,
  requestJson,
  screenshotRequest,
} = await import("../plugin/cua-bridge.mjs")

// ── 工具 ───────────────────────────────────────────────────────────────────

/** 一个跑在回环临时端口上的服务；`hits` 记下每个请求。 */
async function withServer(handler, fn) {
  const hits = []
  const server = http.createServer((req, res) => {
    const chunks = []
    req.on("data", (chunk) => chunks.push(chunk))
    req.on("end", () => {
      const text = Buffer.concat(chunks).toString("utf8")
      const hit = { method: req.method, url: req.url, headers: req.headers, text, body: text === "" ? undefined : JSON.parse(text) }
      hits.push(hit)
      handler(hit, res, req)
    })
  })
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve))
  const baseUrl = `http://127.0.0.1:${server.address().port}`
  try {
    return await fn({ baseUrl, hits })
  } finally {
    server.closeAllConnections?.()
    await new Promise((resolve) => server.close(resolve))
  }
}

/** 一个始终健康的托管实例。 */
function healthy(overrides = {}) {
  return {
    ok: true,
    contractVersion: 1,
    managed: true,
    capabilities: { image: true, compare: true, desktop: false },
    ...overrides,
  }
}

/** 一次合法的 locate-image 响应。 */
function located(matches, image) {
  return {
    ok: true,
    contractVersion: 1,
    coordinateSpace: "image-pixels",
    image,
    matches,
  }
}

const PNG = Buffer.from("not-really-a-png").toString("base64")

/** 一次合法的 compare-images 响应（尺寸相同、可比）。 */
function compared(changed, image = { width: 800, height: 600 }) {
  return {
    ok: true,
    contractVersion: 1,
    comparable: true,
    changed,
    changedPixels: changed ? 12 : 0,
    totalPixels: image.width * image.height,
    changedRatio: changed ? 12 / (image.width * image.height) : 0,
    maxChannelDelta: changed ? 255 : 0,
    changedBox: changed ? { x: 10, y: 20, width: 4, height: 3 } : null,
    image,
    before: image,
    after: image,
  }
}

function shot(data, width = 800, height = 600, windowId = "w1") {
  return { mimeType: "image/png", data, pixelWidth: width, pixelHeight: height, windowId, coordinateSpace: "screenshot-pixels" }
}

function match(text, box, confidence = 0.9) {
  return {
    text,
    confidence,
    box,
    center: { x: box.x + box.width / 2, y: box.y + box.height / 2 },
  }
}

const execution = () => ({ callId: "call-1", agent: { id: "agent-1" }, signal: new AbortController().signal })

/** 一个假的分发面，逐次返回给定结果，并记下每次请求。 */
function fakeDispatch(answers) {
  const calls = []
  return {
    version: 1,
    calls,
    async execute(request, exec) {
      calls.push({ request, execution: exec })
      const next = typeof answers === "function" ? await answers(request, calls.length - 1) : answers.shift()
      if (next instanceof Error) throw next
      return next
    },
  }
}

async function rejects(promise, code) {
  await assert.rejects(promise, (error) => {
    assert.ok(error instanceof BridgeError, `expected BridgeError, got ${error?.name}: ${error?.message}`)
    assert.equal(error.code, code)
    return true
  })
}

// ── loopback 白名单 ────────────────────────────────────────────────────────

test("assertLoopback：只放行本机明文 http", () => {
  for (const url of ["http://127.0.0.1:7788", "http://[::1]:7788", "http://localhost:1"]) {
    assert.equal(assertLoopback(url).hostname.length > 0, true, url)
  }
  assert.throws(() => assertLoopback("https://127.0.0.1:7788"), (error) => error.code === "BAD_ENDPOINT")
  assert.throws(() => assertLoopback("http://192.168.1.10:7788"), (error) => error.code === "BAD_ENDPOINT")
  assert.throws(() => assertLoopback("http://evil.example.com"), (error) => error.code === "BAD_ENDPOINT")
  assert.throws(() => assertLoopback("not a url"), (error) => error.code === "BAD_ENDPOINT")
})

// ── health 握手 ────────────────────────────────────────────────────────────

test("health：契约版本、托管标志、图片能力逐项校验", async () => {
  await withServer((hit, res) => {
    assert.equal(hit.url, "/health")
    res.end(JSON.stringify(healthy()))
  }, async ({ baseUrl }) => {
    const health = await readManagedHealth({ baseUrl })
    assert.equal(health.managed, true)
  })

  const cases = [
    [{ contractVersion: 2 }, "CONTRACT_MISMATCH"],
    [{ contractVersion: undefined }, "CONTRACT_MISMATCH"],
    [{ managed: false }, "CUA_MANAGED_REQUIRED"],
    [{ managed: undefined }, "CUA_MANAGED_REQUIRED"],
    [{ capabilities: { image: false } }, "CAPABILITY_MISSING"],
    [{ capabilities: { image: true, compare: true, desktop: true } }, "CONTRACT_MISMATCH"],
    [{ capabilities: { image: true, compare: true } }, "CONTRACT_MISMATCH"],
  ]
  for (const [overrides, code] of cases) {
    await withServer((_hit, res) => res.end(JSON.stringify(healthy(overrides))), async ({ baseUrl }) => {
      await rejects(readManagedHealth({ baseUrl }), code)
    })
  }
})

test("health：自称托管但桌面路由仍开着 = 变量没进服务进程，不当作可用实例", async () => {
  for (const capabilities of [
    { image: true, compare: true, desktop: true },
    { image: true, compare: true },
  ]) {
    await withServer((_hit, res) => res.end(JSON.stringify(healthy({ capabilities }))), async ({ baseUrl }) => {
      await assert.rejects(readManagedHealth({ baseUrl }), (error) => {
        assert.equal(error.code, "CONTRACT_MISMATCH")
        assert.match(error.message, /desktop routes enabled/, "要说清是「桌面路由还开着」而不是含糊的契约错误")
        assert.match(error.message, /did not reach the service process/)
        assert.match(error.message, /never falls back/)
        return true
      })
    })
  }
})

test("health：非托管实例的错误要说清「重启到托管模式」而不是「重试」", async () => {
  await withServer((_hit, res) => res.end(JSON.stringify(healthy({ managed: false }))), async ({ baseUrl }) => {
    await assert.rejects(readManagedHealth({ baseUrl }), (error) => {
      assert.match(error.message, /UI_LOCATE_CUA_MANAGED=1/)
      assert.match(error.message, /restart/)
      assert.match(error.message, /never falls back/)
      return true
    })
  })
})

// ── 响应形状 ───────────────────────────────────────────────────────────────

test("readLocateResponse：合法响应原样通过", () => {
  const box = { x: 10, y: 20, width: 100, height: 40 }
  const parsed = readLocateResponse(located([match("保存", box)], { width: 800, height: 600 }), { width: 800, height: 600 })
  assert.equal(parsed.matches.length, 1)
  assert.deepEqual(parsed.matches[0].box, box)
  assert.deepEqual(parsed.matches[0].center, { x: 60, y: 40 })
})

test("readLocateResponse：空 matches 是成功结果，不是错误", () => {
  const parsed = readLocateResponse(located([], { width: 800, height: 600 }), { width: 800, height: 600 })
  assert.deepEqual(parsed.matches, [])
})

test("readLocateResponse：坏形状一律 CONTRACT_MISMATCH", () => {
  const image = { width: 800, height: 600 }
  const bad = [
    { ok: false, code: "busy", error: "engine busy" },
    { contractVersion: 2 },
    { coordinateSpace: "screen-pixels" },
    { image: { width: 800, height: 601 } },
    { coordinateSpace: "image-pixels", image, matches: "nope" },
    { coordinateSpace: "image-pixels", image, matches: [{ text: "x", confidence: 0.5, box: null, center: { x: 1, y: 1 } }] },
    // 框出界：宁可拒绝，也不能让 Host 拿一个图外坐标去点
    { coordinateSpace: "image-pixels", image, matches: [match("x", { x: 790, y: 0, width: 100, height: 10 })] },
    // 置信度越界
    { coordinateSpace: "image-pixels", image, matches: [match("x", { x: 0, y: 0, width: 10, height: 10 }, 1.5)] },
    // center 出界
    {
      coordinateSpace: "image-pixels",
      image,
      matches: [{ text: "x", confidence: 0.5, box: { x: 0, y: 0, width: 10, height: 10 }, center: { x: 900, y: 10 } }],
    },
  ]
  for (const payload of bad) {
    assert.throws(() => readLocateResponse({ ok: true, contractVersion: 1, coordinateSpace: "image-pixels", image, matches: [], ...payload }, image),
      (error) => error.code === "CONTRACT_MISMATCH")
  }
})

// ── HTTP 行为 ──────────────────────────────────────────────────────────────

test("HTTP：拒绝跟随重定向", async () => {
  await withServer((_hit, res) => {
    res.writeHead(302, { location: "http://127.0.0.1:1/elsewhere" })
    res.end()
  }, async ({ baseUrl }) => {
    await rejects(requestJson({ baseUrl, path: "/locate-image", body: {} }), "REDIRECT_REFUSED")
  })
})

test("HTTP：响应超过上限就断开，不把整块读进内存", async () => {
  await withServer((_hit, res) => {
    res.writeHead(200, { "content-type": "application/json" })
    res.end(JSON.stringify({ ok: true, padding: "x".repeat(4096) }))
  }, async ({ baseUrl }) => {
    await rejects(requestJson({ baseUrl, path: "/locate-image", body: {}, maxResponseBytes: 1024 }), "RESPONSE_TOO_LARGE")
  })
})

test("HTTP：非 JSON 与错误码都翻成带 code 的 BridgeError", async () => {
  await withServer((_hit, res) => res.end("<html>nope</html>"), async ({ baseUrl }) => {
    await rejects(requestJson({ baseUrl, path: "/locate-image", body: {} }), "BAD_JSON")
  })
  await withServer((_hit, res) => {
    res.writeHead(503, { "content-type": "application/json" })
    res.end(JSON.stringify({ ok: false, code: "busy", error: "engine busy" }))
  }, async ({ baseUrl }) => {
    await rejects(requestJson({ baseUrl, path: "/locate-image", body: {} }), "busy")
  })
})

test("HTTP：总时限到点就放弃（服务端一直不回）", async () => {
  await withServer(() => {
    /* 永不响应 */
  }, async ({ baseUrl }) => {
    await rejects(requestJson({ baseUrl, path: "/locate-image", body: {}, timeoutMs: 60 }), "TIMEOUT")
  })
})

test("HTTP：取消信号立刻打断在途请求", async () => {
  const controller = new AbortController()
  await withServer(() => {
    setTimeout(() => controller.abort(), 10)
  }, async ({ baseUrl }) => {
    await assert.rejects(requestJson({ baseUrl, path: "/locate-image", body: {}, signal: controller.signal, timeoutMs: 5000 }),
      (error) => error.name === "AbortError")
  })
})

test("HTTP：locate 请求体恰好是契约的五个键", async () => {
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    res.end(JSON.stringify(located([], { width: 800, height: 600 })))
  }, async ({ baseUrl, hits }) => {
    const client = new LocateImageClient({ baseUrl })
    const endpoint = await client.resolveEndpoint()
    assert.equal(endpoint.baseUrl, baseUrl)
    await client.locate({ baseUrl: endpoint.baseUrl, imageB64: PNG, width: 800, height: 600, text: "保存" })
    await client.locate({ baseUrl: endpoint.baseUrl, imageB64: PNG, width: 800, height: 600, text: "保存", mode: "detect" })
    assert.deepEqual(hits.map((hit) => hit.url), ["/health", "/locate-image", "/locate-image"])
    assert.deepEqual(Object.keys(hits[1].body).sort(), ["height", "image_b64", "mode", "text", "width"])
    assert.equal(hits[1].body.mode, "locate")
    assert.equal(hits[1].body.text, "保存")
    assert.equal(hits[2].body.mode, "detect")
    assert.equal(hits[2].body.text, "", "detect 模式忽略 text，发空串而不是让服务端 400")
    assert.equal(hits[1].headers["content-type"], "application/json")
  })
})

test("HTTP：空图/空字/坏尺寸在本地就被拒，不发出去", async () => {
  await withServer((_hit, _res) => {}, async ({ baseUrl, hits }) => {
    const client = new LocateImageClient({ baseUrl })
    await rejects(client.locate({ baseUrl, imageB64: "", width: 1, height: 1, text: "x" }), "BAD_REQUEST")
    await rejects(client.locate({ baseUrl, imageB64: PNG, width: 0, height: 1, text: "x" }), "BAD_REQUEST")
    await rejects(client.locate({ baseUrl, imageB64: PNG, width: 1, height: 1, text: "   " }), "BAD_REQUEST")
    await rejects(client.locate({ imageB64: PNG, width: 1, height: 1, text: "x" }), "BAD_REQUEST")
    assert.equal(hits.length, 0)
  })
})

test("候选端点：跳过 legacy 实例，落到托管实例并在本次调用内钉住", async () => {
  // 一个「早于托管模式」的老服务：/health 里没有 contractVersion。
  await withServer((_hit, res) => res.end(JSON.stringify({ ok: true, ready: true, provider: "dml" })), async (legacy) => {
    await withServer((hit, res) => {
      if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
      return res.end(JSON.stringify(located([match("保存", { x: 0, y: 0, width: 10, height: 10 })], { width: 800, height: 600 })))
    }, async (managed) => {
      const dispatch = fakeDispatch([shot(PNG)])
      const client = new LocateImageClient({ baseUrls: [legacy.baseUrl, managed.baseUrl] })
      const result = await operations({ dispatch, client }).locate(target, execution())
      assert.equal(result.matches.length, 1)
      assert.deepEqual(legacy.hits.map((hit) => hit.url), ["/health"], "legacy 只被问过握手，没拿到任何截图")
      assert.deepEqual(managed.hits.map((hit) => hit.url), ["/health", "/locate-image"], "health 与 locate 必须打在同一端点")
      assert.equal("active" in client, false, "端点不许记在实例上——并发调用会互相串端点")
    })
  })
})

test("候选端点：连不上就试下一个", async () => {
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    if (hit.url === "/compare-images") {
      return res.end(JSON.stringify(compared(hit.body.before.image_b64 !== hit.body.after.image_b64)))
    }
    return res.end(JSON.stringify(located([], { width: 800, height: 600 })))
  }, async (managed) => {
    const client = new LocateImageClient({ baseUrls: ["http://127.0.0.1:1", managed.baseUrl] })
    const endpoint = await client.resolveEndpoint()
    assert.equal(endpoint.baseUrl, managed.baseUrl)
    assert.equal(endpoint.health.managed, true)
  })
})

test("候选端点：全部不合格时抛最有信息量的那个，并列出试过什么", async () => {
  await withServer((_hit, res) => res.end(JSON.stringify({ ok: true, ready: true })), async (legacy) => {
    const client = new LocateImageClient({ baseUrls: ["http://127.0.0.1:1", legacy.baseUrl] })
    await assert.rejects(client.resolveEndpoint(), (error) => {
      assert.equal(error.code, "CONTRACT_MISMATCH")
      assert.match(error.message, /no usable ui-locate endpoint/)
      assert.match(error.message, /127\.0\.0\.1:1: TRANSPORT/)
      assert.match(error.message, /CONTRACT_MISMATCH/)
      return true
    })
  })
})

test("端点：并发调用各自拿自己的端点，绝不共享可变状态", async () => {
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    return res.end(JSON.stringify(located([], { width: 800, height: 600 })))
  }, async (first) => {
    await withServer((hit, res) => {
      if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
      return res.end(JSON.stringify(located([], { width: 800, height: 600 })))
    }, async (second) => {
      const client = new LocateImageClient({ baseUrls: [first.baseUrl, second.baseUrl] })
      const call = async () => {
        const endpoint = await client.resolveEndpoint()
        await client.locate({ baseUrl: endpoint.baseUrl, imageB64: PNG, width: 800, height: 600, text: "保存" })
        return endpoint.baseUrl
      }
      const urls = await Promise.all([call(), call(), call()])
      assert.deepEqual(urls, [first.baseUrl, first.baseUrl, first.baseUrl])
      assert.equal(first.hits.filter((hit) => hit.url === "/health").length, 3)
      assert.equal(first.hits.filter((hit) => hit.url === "/locate-image").length, 3)
      assert.equal(second.hits.length, 0, "第二个候选一次都不该被打到")
    })
  })
})

test("compare：请求体是严格白名单形状，尺寸跟着每侧一起发", async () => {
  await withServer((hit, res) => {
    res.end(JSON.stringify(compared(false)))
  }, async ({ baseUrl, hits }) => {
    const client = new LocateImageClient({ baseUrl })
    const result = await client.compare({ baseUrl, beforeB64: PNG, afterB64: PNG, width: 800, height: 600 })
    assert.equal(result.changed, false)
    assert.equal(result.comparable, true)
    assert.deepEqual(Object.keys(hits[0].body).sort(), ["after", "before"])
    assert.deepEqual(hits[0].body.before, { image_b64: PNG, width: 800, height: 600 })
    assert.deepEqual(hits[0].body.after, { image_b64: PNG, width: 800, height: 600 })
  })
})

test("compare：comparable=false 时 changed 必须是 null，不能折叠成 false", async () => {
  const image = { width: 800, height: 600 }
  const base = { ok: true, contractVersion: 1 }
  assert.equal(readCompareResponse({ ...base, comparable: false, changed: null, reason: "dimension_mismatch", image: null }, image).changed, null)
  assert.throws(() => readCompareResponse({ ...base, comparable: false, changed: false }, image), /must be null/)
  assert.throws(() => readCompareResponse({ ...base, comparable: true, changed: null }, image), /must be a boolean/)
  assert.throws(() => readCompareResponse({ ...base, comparable: true, changed: true }, image), /answered for/)
  assert.throws(() => readCompareResponse({ ...base, changed: true }, image), /comparable must be a boolean/)
  assert.throws(() => readCompareResponse({ ...base, contractVersion: 2, comparable: true, changed: true, image }, image), /contractVersion/)
})

// ── 桥接操作 ───────────────────────────────────────────────────────────────

const target = { appHandle: "app", windowId: "w1", text: "保存" }

function operations({ dispatch, client, hits }) {
  return createBridgeOperations({
    getDispatch: () => dispatch,
    client: client ?? new LocateImageClient({ baseUrl: "http://127.0.0.1:1" }),
    logger: { debug: () => {} },
  })
}

test("locate：没有 cuaDispatch 时明确失败，且一个 HTTP 请求都不发", async () => {
  await withServer((_hit, res) => res.end(JSON.stringify(healthy())), async ({ baseUrl, hits }) => {
    const ops = operations({ dispatch: undefined, client: new LocateImageClient({ baseUrl }) })
    await rejects(ops.locate(target, execution()), "CUA_DISPATCH_UNAVAILABLE")
    assert.equal(hits.length, 0, "缺 provider 时不能悄悄走回 Python 端点")
    await assert.rejects(ops.locate(target, execution()), (error) => /will not fall back/.test(error.message))
  })
})

test("locate：分发面版本不符就拒", async () => {
  const dispatch = { version: 2, execute: async () => shot(PNG) }
  await rejects(operations({ dispatch }).locate(target, execution()), "CUA_DISPATCH_VERSION")
})

test("locate：health → 截图 → 定位，返回截图坐标系与匹配", async () => {
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    if (hit.url === "/compare-images") {
      return res.end(JSON.stringify(compared(hit.body.before.image_b64 !== hit.body.after.image_b64)))
    }
    return res.end(JSON.stringify(located([match("保存", { x: 10, y: 20, width: 100, height: 40 })], { width: 800, height: 600 })))
  }, async ({ baseUrl, hits }) => {
    const dispatch = fakeDispatch([shot(PNG)])
    const result = await operations({ dispatch, client: new LocateImageClient({ baseUrl }) }).locate(target, execution())
    assert.deepEqual(hits.map((hit) => hit.url), ["/health", "/locate-image"])
    assert.equal(dispatch.calls.length, 1)
    assert.deepEqual(dispatch.calls[0].request, screenshotRequest({ appHandle: "app", windowId: "w1" }))
    assert.equal(result.route, "cua-screenshot+locate-image")
    assert.equal(result.coordinateSpace, "screenshot-pixels")
    assert.deepEqual(result.image, { width: 800, height: 600 })
    assert.equal(result.matches.length, 1)
    assert.equal(result.selected.center.x, 60)
    assert.equal(result.dispatched, false, "定位不派发输入")
    assert.equal(result.verification, "not-requested")
    assert.equal(result.verified, undefined, "没动作就没有 verified 结论")
  })
})

test("act：无匹配时一次都不点击，dispatched=false / verification=no-match", async () => {
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    if (hit.url === "/compare-images") {
      return res.end(JSON.stringify(compared(hit.body.before.image_b64 !== hit.body.after.image_b64)))
    }
    return res.end(JSON.stringify(located([], { width: 800, height: 600 })))
  }, async ({ baseUrl }) => {
    const dispatch = fakeDispatch([shot(PNG)])
    const result = await operations({ dispatch, client: new LocateImageClient({ baseUrl }) }).act(target, execution())
    assert.equal(dispatch.calls.length, 1, "只有定位用的那一张截图")
    assert.equal(dispatch.calls[0].request.method, "screenshot")
    assert.equal(result.dispatched, false)
    assert.equal(result.verified, false)
    assert.equal(result.verification, "no-match")
    assert.match(result.note, /nothing was dispatched/)
  })
})

test("act：后台点击一次坐标中心，再截图比对，dispatched 与 verified 分开", async () => {
  const box = { x: 10, y: 20, width: 100, height: 40 }
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    if (hit.url === "/compare-images") {
      return res.end(JSON.stringify(compared(hit.body.before.image_b64 !== hit.body.after.image_b64)))
    }
    return res.end(JSON.stringify(located([match("保存", box)], { width: 800, height: 600 })))
  }, async ({ baseUrl }) => {
    const dispatch = fakeDispatch([
      shot(PNG, 800, 600), // 唯一一份观察：定位与点击坐标都来自它
      { dispatched: true, foregroundChanged: null }, // click
      shot(Buffer.from("changed").toString("base64"), 800, 600), // 动作后
    ])
    const result = await operations({ dispatch, client: new LocateImageClient({ baseUrl }) }).act(target, execution())
    assert.deepEqual(dispatch.calls.map((call) => call.request.method), ["screenshot", "click", "screenshot"], "一次动作只用一份观察")
    const click = dispatch.calls[1].request
    assert.deepEqual(click.params, {
      appHandle: "app",
      windowId: "w1",
      x: 60,
      y: 40,
      relative: false,
      mouseButton: "left",
      clickCount: 1,
      deliveryMode: "background",
    })
    assert.equal(result.dispatched, true)
    assert.equal(result.verified, true)
    assert.equal(result.verification, "pixels-changed")
    assert.equal(result.evidence.changed, true)
    assert.equal(result.evidence.kind, "pixel-compare")
    assert.equal(result.evidence.changedPixels, 12)
    assert.match(result.note, /does not prove the task succeeded/)
    assert.equal(result.clicked.deliveryMode, "background")
  })
})

test("act：前台必须显式指定，且原样透传", async () => {
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    if (hit.url === "/compare-images") {
      return res.end(JSON.stringify(compared(hit.body.before.image_b64 !== hit.body.after.image_b64)))
    }
    return res.end(JSON.stringify(located([match("保存", { x: 0, y: 0, width: 10, height: 10 })], { width: 800, height: 600 })))
  }, async ({ baseUrl }) => {
    const dispatch = fakeDispatch([shot(PNG), { dispatched: true, foregroundChanged: true }, shot(PNG)])
    const ops = operations({ dispatch, client: new LocateImageClient({ baseUrl }) })
    await ops.act({ ...target, deliveryMode: "foreground", clickCount: 2 }, execution())
    const click = dispatch.calls[1].request
    assert.equal(click.params.deliveryMode, "foreground")
    assert.equal(click.params.clickCount, 2)
  })
})

test("act：原生不接受时不伪造 dispatched", async () => {
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    if (hit.url === "/compare-images") {
      return res.end(JSON.stringify(compared(hit.body.before.image_b64 !== hit.body.after.image_b64)))
    }
    return res.end(JSON.stringify(located([match("保存", { x: 0, y: 0, width: 10, height: 10 })], { width: 800, height: 600 })))
  }, async ({ baseUrl }) => {
    const dispatch = fakeDispatch([shot(PNG), { dispatched: false }, shot(PNG)])
    const result = await operations({ dispatch, client: new LocateImageClient({ baseUrl }) }).act(target, execution())
    assert.equal(result.dispatched, false, "原生没接受就不能自称派发过")
    assert.equal(result.verified, false, "像素没变就不能说验过")
    assert.equal(result.verification, "pixels-unchanged")
  })
})

test("act：点击后截图尺寸变了 → dimension-changed（尺寸本身是变化的证据）", async () => {
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    if (hit.url === "/compare-images") throw new Error("尺寸不同时不该问 compare")
    return res.end(JSON.stringify(located([match("保存", { x: 0, y: 0, width: 10, height: 10 })], { width: 800, height: 600 })))
  }, async ({ baseUrl, hits }) => {
    const dispatch = fakeDispatch([shot(PNG, 800, 600), { dispatched: true }, shot(PNG, 640, 480)])
    const result = await operations({ dispatch, client: new LocateImageClient({ baseUrl }) }).act(target, execution())
    assert.equal(result.verified, true)
    assert.equal(result.verification, "dimension-changed")
    assert.equal(result.evidence.dimensionChanged, true)
    assert.equal(hits.filter((hit) => hit.url === "/compare-images").length, 0, "尺寸不同不归 compare 判断，也不该发这个请求")
  })
})

test("act：点击后截图属于另一个窗口 → WINDOW_CHANGED，证据不可信", async () => {
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    return res.end(JSON.stringify(located([match("保存", { x: 0, y: 0, width: 10, height: 10 })], { width: 800, height: 600 })))
  }, async ({ baseUrl }) => {
    const dispatch = fakeDispatch([shot(PNG, 800, 600, "w1"), { dispatched: true }, shot(PNG, 800, 600, "w2")])
    await rejects(operations({ dispatch, client: new LocateImageClient({ baseUrl }) }).act(target, execution()), "WINDOW_CHANGED")
  })
})

test("act：缺 capabilities.compare 就不点击（没有像素证据就不派发）", async () => {
  await withServer((hit, res) => {
    if (hit.url === "/health") {
      return res.end(JSON.stringify({ ...healthy(), capabilities: { image: true, desktop: false } }))
    }
    return res.end(JSON.stringify(located([match("保存", { x: 0, y: 0, width: 10, height: 10 })], { width: 800, height: 600 })))
  }, async ({ baseUrl }) => {
    const dispatch = fakeDispatch([shot(PNG)])
    const result = await operations({ dispatch, client: new LocateImageClient({ baseUrl }) }).act(target, execution())
    assert.deepEqual(dispatch.calls.map((call) => call.request.method), ["screenshot"], "没有比较能力就只截图，不点击")
    assert.equal(result.dispatched, false)
    assert.equal(result.verified, null)
    assert.equal(result.verification, "image-compare-unavailable")
    assert.match(result.note, /capabilities.compare/)
  })
})

test("act：compare 本身失败时 verified=null（未知，不是「没变化」）", async () => {
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    if (hit.url === "/compare-images") {
      res.statusCode = 500
      return res.end(JSON.stringify({ ok: false, code: "engine_error", error: "boom" }))
    }
    return res.end(JSON.stringify(located([match("保存", { x: 0, y: 0, width: 10, height: 10 })], { width: 800, height: 600 })))
  }, async ({ baseUrl }) => {
    const dispatch = fakeDispatch([shot(PNG), { dispatched: true }, shot(Buffer.from("changed").toString("base64"))])
    const result = await operations({ dispatch, client: new LocateImageClient({ baseUrl }) }).act(target, execution())
    assert.equal(result.dispatched, true, "输入确实派发了")
    assert.equal(result.verified, null, "比不了就是未知")
    assert.equal(result.verification, "image-compare-unavailable")
    assert.equal(result.evidence.error.code, "engine_error", "服务端的错误码原样带回来，不泛化")
  })
})

test("act：原生报 BACKGROUND_UNAVAILABLE 就原样上抛，不偷偷切前台", async () => {
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    if (hit.url === "/compare-images") {
      return res.end(JSON.stringify(compared(hit.body.before.image_b64 !== hit.body.after.image_b64)))
    }
    return res.end(JSON.stringify(located([match("保存", { x: 0, y: 0, width: 10, height: 10 })], { width: 800, height: 600 })))
  }, async ({ baseUrl }) => {
    const failure = Object.assign(new Error("BACKGROUND_UNAVAILABLE"), { code: "BACKGROUND_UNAVAILABLE" })
    const dispatch = fakeDispatch([shot(PNG), failure])
    await assert.rejects(operations({ dispatch, client: new LocateImageClient({ baseUrl }) }).act(target, execution()),
      (error) => error.code === "BACKGROUND_UNAVAILABLE")
    assert.equal(dispatch.calls.length, 2, "失败之后不再为「前台重试」补一次调用")
    assert.equal(dispatch.calls.some((call) => call.request.params?.deliveryMode === "foreground"), false)
  })
})

test("act：取消之后不再发截图或点击", async () => {
  const controller = new AbortController()
  await withServer((hit, res) => {
    if (hit.url === "/health") return res.end(JSON.stringify(healthy()))
    if (hit.url === "/compare-images") {
      return res.end(JSON.stringify(compared(hit.body.before.image_b64 !== hit.body.after.image_b64)))
    }
    return res.end(JSON.stringify(located([match("保存", { x: 0, y: 0, width: 10, height: 10 })], { width: 800, height: 600 })))
  }, async ({ baseUrl }) => {
    const dispatch = fakeDispatch((request) => {
      if (request.method === "click") {
        controller.abort()
        return { dispatched: true }
      }
      return shot(PNG)
    })
    await assert.rejects(
      operations({ dispatch, client: new LocateImageClient({ baseUrl }) }).act(target, { ...execution(), signal: controller.signal }),
      (error) => error.name === "AbortError",
    )
    assert.deepEqual(dispatch.calls.map((call) => call.request.method), ["screenshot", "click"], "取消之后不许再截图")
  })
})

test("act：执行上下文缺失时拒绝（不接受伪造的 execution）", async () => {
  const dispatch = fakeDispatch([])
  await rejects(operations({ dispatch }).act(target, {}), "EXECUTION_REQUIRED")
  await rejects(operations({ dispatch }).act(target, { callId: "c", agent: { id: "a" } }), "EXECUTION_REQUIRED")
  assert.equal(dispatch.calls.length, 0)
})

test("clickRequest：坐标是截图像素，显式声明 relative=false", () => {
  const request = clickRequest({ appHandle: "app", windowId: "w1", x: 12, y: 34 })
  assert.deepEqual(request, {
    method: "click",
    params: { appHandle: "app", windowId: "w1", x: 12, y: 34, relative: false, mouseButton: "left", clickCount: 1, deliveryMode: "background" },
  })
})

// ── Host 接线 ──────────────────────────────────────────────────────────────

/** 与 update-plugin.test.mjs 同形的假 ctx，外加可设置的 `cuaDispatch`。 */
function fakeCtx() {
  const tools = []
  const services = new Map()
  const ctx = {
    logger: { info: () => {}, debug: () => {} },
    tools: { register: (tool) => tools.push(tool) },
    inject(deps, callback) {
      if (deps.includes("webServer")) {
        callback({ webServer: { register: () => () => {} }, get: () => undefined, effect: (fn) => void fn() })
      }
    },
    on: () => () => {},
    effect(fn) {
      const dispose = fn()
      return typeof dispose === "function" ? dispose : () => {}
    },
    get(name) {
      return services.get(name)
    },
    _services: services,
    _tools: tools,
  }
  return ctx
}

test("index：注册两个 CUA 桥接工具，入参与默认值是后台投递", async () => {
  const plugin = await import("../plugin/index.mjs")
  const ctx = fakeCtx()
  plugin.apply(ctx, plugin.Config({ pythonSync: false }))
  const byName = new Map(ctx._tools.map((tool) => [tool.name, tool]))
  for (const name of ["ui_locate_cua_locate", "ui_locate_cua_act"]) {
    const tool = byName.get(name)
    assert.ok(tool, `缺少工具 ${name}`)
    assert.deepEqual(tool.parameters.required, ["appHandle", "windowId", "text"])
    assert.equal(tool.parameters.additionalProperties, false)
    assert.match(tool.description, /不点击|后台/, `${name} 的描述要讲清它默认不抢焦点`)
  }
  const act = byName.get("ui_locate_cua_act")
  assert.match(act.description, /BACKGROUND|不支持后台/)
  assert.ok(act.parameters.properties.deliveryMode, "act 必须能显式选 foreground")
})

test("index：没有 cuaDispatch 时两个工具都明确失败，不回落到 Python", async () => {
  const plugin = await import("../plugin/index.mjs")
  const ctx = fakeCtx()
  plugin.apply(ctx, plugin.Config({ pythonSync: false }))
  const byName = new Map(ctx._tools.map((tool) => [tool.name, tool]))
  const args = { appHandle: "app", windowId: "w1", text: "保存" }
  for (const name of ["ui_locate_cua_locate", "ui_locate_cua_act"]) {
    await assert.rejects(byName.get(name).execute(args, execution()), (error) => {
      assert.equal(error.code, "CUA_DISPATCH_UNAVAILABLE")
      return true
    })
  }
})

test("index：cuaDispatch 被撤销（get 不再返回）之后立刻不可用", async () => {
  const plugin = await import("../plugin/index.mjs")
  const ctx = fakeCtx()
  plugin.apply(ctx, plugin.Config({ pythonSync: false }))
  const locate = ctx._tools.find((tool) => tool.name === "ui_locate_cua_locate")
  // 指向一个没人监听的端口：本测试只关心「分发面在不在」，不碰本机可能存在的 7788。
  const previous = process.env.UI_LOCATE_BASE_URL
  process.env.UI_LOCATE_BASE_URL = "http://127.0.0.1:1"
  try {
    ctx._services.set("cuaDispatch", { version: 1, execute: async () => shot(PNG) })
    // 分发面在 → 越过「缺 provider」，下一步连不上端点，报传输层错误。
    await assert.rejects(locate.execute({ appHandle: "app", windowId: "w1", text: "x" }, execution()),
      (error) => error.code === "TRANSPORT")
    ctx._services.delete("cuaDispatch")
    await assert.rejects(locate.execute({ appHandle: "app", windowId: "w1", text: "x" }, execution()),
      (error) => error.code === "CUA_DISPATCH_UNAVAILABLE")
  } finally {
    if (previous === undefined) delete process.env.UI_LOCATE_BASE_URL
    else process.env.UI_LOCATE_BASE_URL = previous
  }
})

// ── I3：与 C 的真实端点实连（可选） ────────────────────────────────────────
//
// 这一层不用任何假服务：客户端打的是 C 那份真 Handler + 真校验/解码，只有 OCR 引擎换替身。
// 起服务的方式见 _skadi_temp/i3-harness/run-live.ps1（或 C 的 tests/stub_locate_image.py），
// 然后把地址与图片路径塞进环境变量：
//   UI_LOCATE_LIVE_URL       http://127.0.0.1:<port>
//   UI_LOCATE_LIVE_IMAGE     先拍的 PNG 路径
//   UI_LOCATE_LIVE_IMAGE_B   点击后再拍的 PNG 路径（可选，用于「像素真的变了」）
//   UI_LOCATE_LIVE_WIDTH / UI_LOCATE_LIVE_HEIGHT  图片尺寸（默认 200×120）

const LIVE_URL = process.env.UI_LOCATE_LIVE_URL
const LIVE_IMAGE = process.env.UI_LOCATE_LIVE_IMAGE
const LIVE_IMAGE_B = process.env.UI_LOCATE_LIVE_IMAGE_B
const liveSkip = LIVE_URL && LIVE_IMAGE
  ? false
  : "未设置 UI_LOCATE_LIVE_URL / UI_LOCATE_LIVE_IMAGE（用 C 的 tests/stub_locate_image.py 起真服务后传入）"

function liveImage(path) {
  const bytes = readFileSync(path)
  return {
    b64: bytes.toString("base64"),
    width: Number(process.env.UI_LOCATE_LIVE_WIDTH ?? 200),
    height: Number(process.env.UI_LOCATE_LIVE_HEIGHT ?? 120),
  }
}

test("I3 实连：真实 /health + /locate-image 契约（C 的 handler）", { skip: liveSkip }, async () => {
  const image = liveImage(LIVE_IMAGE)
  const client = new LocateImageClient({ baseUrl: LIVE_URL })
  const endpoint = await client.resolveEndpoint()
  assert.equal(endpoint.health.contractVersion, 1)
  assert.equal(endpoint.health.managed, true)
  assert.equal(endpoint.health.capabilities.compare, true, "Host 要在点击前确认 compare 能力")

  const located = await client.locate({
    baseUrl: endpoint.baseUrl, imageB64: image.b64, width: image.width, height: image.height, text: "Save",
  })
  assert.equal(located.matches.length, 1)
  const [first] = located.matches
  assert.equal(first.text, "Save")
  assert.equal(first.confidence, 0.95)
  assert.deepEqual(first.box, { x: 40, y: 30, width: 80, height: 30 })
  assert.deepEqual(first.center, { x: 80, y: 45 })

  // 取消：在途请求真的断掉，而且不会把服务弄坏（取消后还能再握手）。
  const controller = new AbortController()
  const pending = client.locate({
    baseUrl: endpoint.baseUrl, imageB64: image.b64, width: image.width, height: image.height, text: "Save", signal: controller.signal,
  })
  controller.abort()
  await assert.rejects(pending, (error) => error.name === "AbortError")
  assert.equal((await client.resolveEndpoint()).health.managed, true, "取消一次请求不能把服务搞坏")
})

test("I3 实连：真实 /compare-images 给出像素结论，尺寸不符不判成功", { skip: liveSkip }, async () => {
  const before = liveImage(LIVE_IMAGE)
  const client = new LocateImageClient({ baseUrl: LIVE_URL })
  const endpoint = await client.resolveEndpoint()

  const same = await client.compare({ baseUrl: endpoint.baseUrl, beforeB64: before.b64, afterB64: before.b64, width: before.width, height: before.height })
  assert.equal(same.comparable, true)
  assert.equal(same.changed, false, "同一张图必须说没变")

  if (LIVE_IMAGE_B) {
    const after = liveImage(LIVE_IMAGE_B)
    assert.deepEqual([after.width, after.height], [before.width, before.height], "这组用例要求两张图同尺寸")
    const diff = await client.compare({ baseUrl: endpoint.baseUrl, beforeB64: before.b64, afterB64: after.b64, width: before.width, height: before.height })
    assert.equal(diff.comparable, true)
    assert.equal(diff.changed, true, "像素变了就必须说变了")
    assert.ok(diff.changedPixels > 0)
  }
})

const liveActSkip = liveSkip ? liveSkip : (LIVE_IMAGE_B ? false : "需要 UI_LOCATE_LIVE_IMAGE_B（点击后的截图）")

test("I3 实连：act 用同一份真实截图定位、点击，再用真实像素比较给证据", { skip: liveActSkip }, async () => {
  const before = liveImage(LIVE_IMAGE)
  const after = liveImage(LIVE_IMAGE_B)
  const dispatch = fakeDispatch([
    shot(before.b64, before.width, before.height),
    { dispatched: true, foregroundChanged: null },
    shot(after.b64, after.width, after.height),
  ])
  const client = new LocateImageClient({ baseUrl: LIVE_URL })
  const result = await operations({ dispatch, client }).act({ ...target, text: "Save" }, execution())
  assert.deepEqual(dispatch.calls.map((call) => call.request.method), ["screenshot", "click", "screenshot"], "一次动作用一份观察")
  const click = dispatch.calls[1].request
  assert.equal(click.params.x, 80)
  assert.equal(click.params.y, 45)
  assert.equal(click.params.deliveryMode, "background")
  assert.equal(result.dispatched, true)
  assert.equal(result.verified, true)
  assert.equal(result.verification, "pixels-changed")
  assert.ok(result.evidence.changedPixels > 0, "证据必须来自真实解码像素")
})
const LIVE_EMPTY_URL = process.env.UI_LOCATE_LIVE_EMPTY_URL
const LIVE_ERROR_URL = process.env.UI_LOCATE_LIVE_ERROR_URL
const emptySkip = liveSkip ? liveSkip : (LIVE_EMPTY_URL ? false : "需要 UI_LOCATE_LIVE_EMPTY_URL（--scenario empty 的实例）")
const errorSkip = liveSkip ? liveSkip : (LIVE_ERROR_URL ? false : "需要 UI_LOCATE_LIVE_ERROR_URL（--scenario error 的实例）")

test("I3 实连：空结果是「成功的空 matches」，不是错误", { skip: emptySkip }, async () => {
  const image = liveImage(LIVE_IMAGE)
  const client = new LocateImageClient({ baseUrl: LIVE_EMPTY_URL })
  const endpoint = await client.resolveEndpoint()
  const located = await client.locate({
    baseUrl: endpoint.baseUrl, imageB64: image.b64, width: image.width, height: image.height, text: "Save",
  })
  assert.deepEqual(located.matches, [], "空结果照契约是 200 + 空数组，Host 靠它决定「不点击」")
})

test("I3 实连：引擎失败按契约回错误码，不会被当成空结果", { skip: errorSkip }, async () => {
  const image = liveImage(LIVE_IMAGE)
  const client = new LocateImageClient({ baseUrl: LIVE_ERROR_URL })
  const endpoint = await client.resolveEndpoint()
  await assert.rejects(client.locate({
    baseUrl: endpoint.baseUrl, imageB64: image.b64, width: image.width, height: image.height, text: "Save",
  }), (error) => error.code === "engine_error", "失败必须显式冒出来，不能伪造匹配也不能伪装成「没找到」")
})