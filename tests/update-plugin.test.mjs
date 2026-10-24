/**
 * 更新插件的测试。
 *
 * 跑法：`node --test tests/`（在包根目录下）。
 *
 * 覆盖两层：
 *  - `plugin/core.mjs`：纯逻辑，外部世界（fetch / exec / 时钟）全部注入，
 *    所以这里能测到失败路径、超时、以及「uv 不存在时退回 pip」这类分支。
 *  - `plugin/index.mjs`：接线层。用最小桩顶替 `@deepseek-ai/dsh-tools` 与
 *    `@deepseek-ai/schemastery`（见 `node_modules/` 下那两个桩），然后拿一个
 *    假 ctx 去跑 `apply()`，确认工具真的注册了、inject 声明对了、开关真的
 *    会停掉 timer。
 *
 * 一条纪律：**任何测试都不许打真实网络**。默认的 `fetch` 已经被
 * `DSH_UI_LOCATE_UPDATE_API` 指到 `127.0.0.1:9`，所有检查都被显式传
 * `fetch` 参数接管。
 */

import assert from "node:assert/strict"
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"

// 必须在 import plugin 之前设：index.mjs 在 apply 时读它。
process.env.DSH_UI_LOCATE_UPDATE_API = "http://127.0.0.1:9"
process.env.DSH_UI_LOCATE_UPDATE_DELAY_MS = "600000"

const ROOT = fileURLToPath(new URL("..", import.meta.url))

// 两个 DSH 内置包由 Host 提供，本仓库不安装它们；先把桩装好，index.mjs 才
// import 得动。桩不进版本控制（node_modules/ 在 .gitignore 里）。
const { ensureStubs } = await import("./stubs.mjs")
ensureStubs(ROOT)

const {
  UPDATE_REPOSITORY,
  bundledPythonVersion,
  compareVersions,
  createUpdateTracker,
  fetchReleaseTags,
  installIntoVenv,
  installedPythonVersion,
  latestRelease,
  packageVersion,
  parseReleaseTag,
  stateDir,
  syncPythonToBundle,
  updateVerdict,
  venvPython,
} = await import("../plugin/core.mjs")

// ── 工具 ───────────────────────────────────────────────────────────────────

let passed = 0
let failed = 0

/** 一个临时目录，用完删掉。 */
function sandbox(fn) {
  const dir = mkdtempSync(join(tmpdir(), "ui-locate-test-"))
  try {
    return fn(dir)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

/** 等一个 Promise 走完微任务链（tracker 的 finally 要两跳）。 */
const settle = () => new Promise((resolve) => setImmediate(resolve))

/** 一个假的 fetch：按序返回给定响应。 */
function mockFetch(...answers) {
  const calls = []
  const fn = async (url, init) => {
    calls.push({ url, init })
    const next = answers.shift()
    if (next instanceof Error) throw next
    if (typeof next === "function") return next(url, init)
    return {
      ok: next.status === undefined || (next.status >= 200 && next.status < 300),
      status: next.status ?? 200,
      json: async () => next.body,
    }
  }
  fn.calls = calls
  return fn
}

/** 一个假的 exec：记下调用，按给定行为回话。 */
function mockExec(behavior) {
  const calls = []
  const fn = (command, args, options, callback) => {
    calls.push({ command, args, options })
    const answer = behavior(command, args)
    setImmediate(() => {
      if (answer?.error) callback(Object.assign(new Error(answer.error), { code: answer.code }), "", answer.stderr ?? "")
      else callback(null, answer?.stdout ?? "", "")
    })
  }
  fn.calls = calls
  return fn
}

/** 跑一个用例并记账，顺便在失败时打印名字（node --test 也会打，但这样更清楚）。 */
function check(name, fn) {
  test(name, async () => {
    try {
      await fn()
      passed += 1
    } catch (error) {
      failed += 1
      throw error
    }
  })
}

// ── 版本比较 ───────────────────────────────────────────────────────────────

check("parseReleaseTag 只认 vX.Y.Z", () => {
  assert.deepEqual(parseReleaseTag("v1.2.3"), [1, 2, 3])
  assert.deepEqual(parseReleaseTag("v0.0.0"), [0, 0, 0])
  assert.deepEqual(parseReleaseTag("  v10.20.30  "), [10, 20, 30])
  for (const bad of ["1.2.3", "v1.2", "v1.2.3.4", "v01.2.3", "v1.2.3-rc.1", "v1.2.x", "", "v", null, undefined]) {
    assert.equal(parseReleaseTag(bad), undefined, `应当拒绝 ${JSON.stringify(bad)}`)
  }
})

check("compareVersions 逐段比较，带不带 v 都行", () => {
  assert.ok(compareVersions("1.0.0", "1.0.1") < 0)
  assert.ok(compareVersions("v1.2.0", "1.10.0") < 0, "数值比较，不是字典序")
  assert.equal(compareVersions("1.2.3", "v1.2.3"), 0)
  assert.ok(compareVersions("2.0.0", "1.99.99") > 0)
})

check("compareVersions 把解析不出的排到后面", () => {
  assert.ok(compareVersions("1.0.0", "wat") < 0)
  assert.ok(compareVersions("wat", "1.0.0") > 0)
  assert.equal(compareVersions("a", "b"), 0)
})

check("latestRelease 忽略预发布与奇怪标签", () => {
  assert.equal(latestRelease(["v1.0.0", "v1.2.0", "v1.1.9"]), "v1.2.0")
  assert.equal(latestRelease(["v1.0.0", "v2.0.0-rc.1", "nightly"]), "v1.0.0")
  assert.equal(latestRelease(["nightly", "v0.0.1"]), "v0.0.1")
  assert.equal(latestRelease([]), undefined)
  assert.equal(latestRelease(["nightly"]), undefined)
})

check("updateVerdict 分清 available 与 current", () => {
  assert.deepEqual(updateVerdict(["v1.0.0", "v1.1.0"], "1.0.0"), { state: "available", latest: "v1.1.0" })
  assert.deepEqual(updateVerdict(["v1.0.0"], "1.0.0"), { state: "current", latest: "v1.0.0" })
  assert.deepEqual(updateVerdict(["v0.9.0"], "1.0.0"), { state: "current", latest: "v0.9.0" })
  assert.deepEqual(updateVerdict([], "1.0.0"), { state: "current" })
})

// ── 读元数据 ───────────────────────────────────────────────────────────────

check("packageVersion 与 pyproject 的版本一致", () => {
  const npm = packageVersion()
  const py = bundledPythonVersion()
  assert.match(npm, /^\d+\.\d+\.\d+$/)
  assert.equal(py, npm, "两个版本必须同步，否则每次启动都会重装 Python 侧")
})

check("bundledPythonVersion 只在 [project] 段里找", () => {
  sandbox((dir) => {
    writeFileSync(
      join(dir, "pyproject.toml"),
      [
        "[build-system]",
        'requires = ["setuptools>=68"]',
        "",
        "[tool.something]",
        'version = "9.9.9"',
        "",
        "[project]",
        'name = "x"',
        'version = "1.2.3"',
        "",
        "[tool.other]",
        'version = "8.8.8"',
      ].join("\n"),
    )
    assert.equal(bundledPythonVersion(dir), "1.2.3")
  })
})

check("bundledPythonVersion 读不到时返回 undefined", () => {
  sandbox((dir) => {
    assert.equal(bundledPythonVersion(dir), undefined)
    writeFileSync(join(dir, "pyproject.toml"), "[project]\nname = 'x'\n")
    assert.equal(bundledPythonVersion(dir), undefined)
  })
})

// ── 运行环境位置 ───────────────────────────────────────────────────────────

check("stateDir 的优先级与 cordis.patch.yml 一致", () => {
  assert.equal(stateDir({ UI_LOCATE_HOME: "E:/a", DSH_HOME: "D:/data" }), "E:/a")
  assert.equal(stateDir({ DSH_HOME: "D:/data" }), "D:/data/ui-locate")
  assert.equal(stateDir({ LOCALAPPDATA: "C:/Local" }), "C:/Local/ui-locate")
  assert.equal(stateDir({ HOME: "/home/x" }), "/home/x/.local/share/ui-locate")
  const fallback = stateDir({})
  assert.ok(fallback.endsWith("/ui-locate"), fallback)
})

check("venvPython 按平台分叉", () => {
  assert.equal(venvPython("D:/h", "win32"), "D:/h/venv/Scripts/python.exe")
  assert.equal(venvPython("/h", "linux"), "/h/venv/bin/python")
})

// ── Python 侧 ──────────────────────────────────────────────────────────────

check("installedPythonVersion：venv 不存在就不跑子进程", async () => {
  const exec = mockExec(() => ({ stdout: "1.0.0" }))
  const answer = await installedPythonVersion("D:/nope/python.exe", { exec, exists: () => false })
  assert.equal(answer, undefined)
  assert.equal(exec.calls.length, 0, "不该白起一个进程")
})

check("installedPythonVersion：读回版本", async () => {
  const exec = mockExec((command, args) => {
    assert.match(command, /python\.exe$/)
    assert.match(args.join(" "), /importlib\.metadata/)
    return { stdout: "0.2.0\n" }
  })
  assert.equal(await installedPythonVersion("D:/h/venv/Scripts/python.exe", { exec, exists: () => true }), "0.2.0")
})

check("installedPythonVersion：包没装好时返回 undefined，不抛", async () => {
  const exec = mockExec(() => ({ error: "Command failed: No package metadata was found", code: 1 }))
  assert.equal(await installedPythonVersion("D:/h/venv/Scripts/python.exe", { exec, exists: () => true }), undefined)
})

check("installIntoVenv：优先 uv", async () => {
  const exec = mockExec((command) => (command === "uv" ? { stdout: "ok" } : { error: "unexpected" }))
  await installIntoVenv("D:/h/py.exe", "D:/pkg[cuda]", { exec })
  assert.equal(exec.calls.length, 1)
  assert.equal(exec.calls[0].command, "uv")
  assert.deepEqual(exec.calls[0].args, ["pip", "install", "--python", "D:/h/py.exe", "D:/pkg[cuda]"])
})

check("installIntoVenv：没有 uv 时退回 venv 自己的 pip", async () => {
  const exec = mockExec((command) => {
    if (command === "uv") return { error: "spawn uv ENOENT", code: "ENOENT" }
    return { stdout: "ok" }
  })
  await installIntoVenv("D:/h/py.exe", "D:/pkg", { exec })
  assert.equal(exec.calls.length, 2)
  assert.equal(exec.calls[1].command, "D:/h/py.exe")
  assert.deepEqual(exec.calls[1].args, ["-m", "pip", "install", "--upgrade", "D:/pkg"])
})

check("installIntoVenv：uv 真的装失败时不许悄悄退回 pip", async () => {
  const exec = mockExec(() => ({ error: "ERROR: Could not find a version that satisfies the requirement", code: 1 }))
  await assert.rejects(() => installIntoVenv("D:/h/py.exe", "D:/pkg", { exec }))
  assert.equal(exec.calls.length, 1, "装失败就停，别用 pip 再赌一次")
})

check("syncPythonToBundle：venv 不存在时只说 missing，不动手", async () => {
  const exec = mockExec(() => ({ stdout: "" }))
  const answer = await syncPythonToBundle({
    root: ROOT,
    home: "D:/h",
    platform: "win32",
    exec,
    exists: () => false,
  })
  assert.equal(answer.state, "missing")
  assert.equal(exec.calls.length, 0)
})

check("syncPythonToBundle：版本相同就什么都不做", async () => {
  const exec = mockExec(() => ({ stdout: `${packageVersion()}\n` }))
  const answer = await syncPythonToBundle({
    root: ROOT,
    home: "D:/h",
    platform: "win32",
    exec,
    exists: () => true,
  })
  assert.deepEqual(answer, { state: "current", version: packageVersion() })
  assert.equal(exec.calls.length, 1, "只问一次版本，不该有安装")
})

check("syncPythonToBundle：版本不同就重装并复查", async () => {
  const want = packageVersion()
  let installed = false
  const exec = mockExec((command, args) => {
    // 只有 uv 那一步会「装上」，之后复查就能读到新版本。
    if (command === "uv") {
      installed = true
      return { stdout: "ok" }
    }
    assert.match(args.join(" "), /importlib\.metadata/)
    return { stdout: `${installed ? want : "0.0.1"}\n` }
  })
  const answer = await syncPythonToBundle({
    root: ROOT,
    home: "D:/h",
    platform: "win32",
    exec,
    exists: () => true,
    cpuOnly: true,
  })
  assert.deepEqual(answer, { state: "updated", version: want })
  const install = exec.calls.find((call) => call.command === "uv")
  assert.ok(install, "应当用 uv 装")
  // ROOT 是目录 URL 转来的，末尾带斜杠；target 是去掉斜杠之后拼的。
  assert.equal(install.args.at(-1), ROOT.replace(/[\\/]+$/, ""), "cpuOnly 时不许带 [cuda]")
})

check("syncPythonToBundle：cuda extras 跟着 cpuOnly 走", async () => {
  const want = packageVersion()
  let installed = false
  const exec = mockExec((command, args) => {
    if (command === "uv") {
      installed = true
      return { stdout: "ok" }
    }
    assert.match(args.join(" "), /importlib\.metadata/)
    return { stdout: `${installed ? want : "0.0.1"}\n` }
  })
  await syncPythonToBundle({ root: ROOT, home: "D:/h", platform: "win32", exec, exists: () => true, cpuOnly: false })
  const install = exec.calls.find((call) => call.command === "uv")
  assert.ok(install.args.at(-1).endsWith("[cuda]"), install.args.at(-1))
})

check("syncPythonToBundle：装完版本还是不对要如实说", async () => {
  const exec = mockExec(() => ({ stdout: "0.0.1\n" }))
  const answer = await syncPythonToBundle({
    root: ROOT,
    home: "D:/h",
    platform: "win32",
    exec,
    exists: () => true,
    cpuOnly: true,
  })
  assert.equal(answer.state, "mismatch")
  assert.equal(answer.have, "0.0.1")
})

check("syncPythonToBundle：安装失败要带错误信息，不抛", async () => {
  const exec = mockExec((command, args) => {
    if (args?.[1]?.includes?.("importlib")) return { stdout: "0.0.1\n" }
    if (command === "uv") return { error: "spawn uv ENOENT", code: "ENOENT" }
    return { error: "ERROR: network unreachable", code: 1 }
  })
  const answer = await syncPythonToBundle({
    root: ROOT,
    home: "D:/h",
    platform: "win32",
    exec,
    exists: () => true,
    cpuOnly: true,
  })
  assert.equal(answer.state, "failed")
  assert.match(answer.error, /network unreachable/)
})

// ── 发布源 ─────────────────────────────────────────────────────────────────

check("fetchReleaseTags：读公开 tag 列表，不带凭据", async () => {
  const fetch = mockFetch({ body: [{ name: "v1.0.0" }, { name: "v0.9.0" }, { notAName: true }] })
  const tags = await fetchReleaseTags({ fetch })
  assert.deepEqual(tags, ["v1.0.0", "v0.9.0"])
  assert.equal(fetch.calls.length, 1)
  assert.equal(fetch.calls[0].url, `https://api.github.com/repos/${UPDATE_REPOSITORY}/tags?per_page=100`)
  const headers = fetch.calls[0].init.headers
  assert.equal(headers.accept, "application/vnd.github+json")
  assert.equal(headers["x-github-api-version"], "2022-11-28")
  assert.equal(headers.authorization, undefined, "这里绝不该带凭据")
})

check("fetchReleaseTags：限流与非 200 分开报", async () => {
  await assert.rejects(
    () => fetchReleaseTags({ fetch: mockFetch({ status: 403, body: {} }) }),
    /GitHub rate limit \(HTTP 403\)/,
  )
  await assert.rejects(
    () => fetchReleaseTags({ fetch: mockFetch({ status: 429, body: {} }) }),
    /GitHub rate limit \(HTTP 429\)/,
  )
  await assert.rejects(() => fetchReleaseTags({ fetch: mockFetch({ status: 500, body: {} }) }), /HTTP 500/)
})

check("fetchReleaseTags：返回的不是数组就报错", async () => {
  await assert.rejects(() => fetchReleaseTags({ fetch: mockFetch({ body: { message: "Not Found" } }) }), /tag list/)
})

check("fetchReleaseTags：可以指到自建镜像", async () => {
  const fetch = mockFetch({ body: [] })
  await fetchReleaseTags({ fetch, api: "http://mirror.local/api/", repository: "a/b" })
  assert.equal(fetch.calls[0].url, "http://mirror.local/api/repos/a/b/tags?per_page=100")
})

// ── 更新状态机 ─────────────────────────────────────────────────────────────

check("tracker：有更新时报 available", async () => {
  const tracker = createUpdateTracker({
    readVersion: () => "1.0.0",
    fetchTags: async () => ["v1.0.0", "v1.1.0"],
  })
  await tracker.check()
  const state = tracker.state
  assert.equal(state.state, "available")
  assert.equal(state.latest, "v1.1.0")
  assert.match(state.checkedAt, /^\d{4}-\d{2}-\d{2}T/)
  tracker.dispose()
})

check("tracker：已是最新时报 current", async () => {
  const tracker = createUpdateTracker({ readVersion: () => "1.1.0", fetchTags: async () => ["v1.0.0", "v1.1.0"] })
  await tracker.check()
  assert.equal(tracker.state.state, "current")
  tracker.dispose()
})

check("tracker：网络失败也不抛，并且记住上次查到的新版本", async () => {
  const answers = [["v1.1.0"], new Error("getaddrinfo ENOTFOUND api.github.com")]
  const tracker = createUpdateTracker({
    readVersion: () => "1.0.0",
    fetchTags: async () => {
      const next = answers.shift()
      if (next instanceof Error) throw next
      return next
    },
  })
  await tracker.check()
  assert.equal(tracker.state.state, "available")
  await tracker.check()
  const state = tracker.state
  assert.equal(state.state, "failed")
  assert.equal(state.stale, "available", "已知的新版本不该被一次抖动抹掉")
  assert.equal(state.latest, "v1.1.0")
  assert.match(state.error, /ENOTFOUND/)
  tracker.dispose()
})

check("tracker：并发 check 只发一个请求", async () => {
  let calls = 0
  const tracker = createUpdateTracker({
    readVersion: () => "1.0.0",
    fetchTags: async () => {
      calls += 1
      await new Promise((resolve) => setTimeout(resolve, 20))
      return ["v1.1.0"]
    },
  })
  await Promise.all([tracker.check(), tracker.check(), tracker.check()])
  assert.equal(calls, 1)
  tracker.dispose()
})

check("tracker：自己版本读不出来时立刻失败，不发请求", async () => {
  let calls = 0
  const tracker = createUpdateTracker({
    readVersion: () => undefined,
    fetchTags: async () => {
      calls += 1
      return []
    },
  })
  await tracker.check()
  assert.equal(tracker.state.state, "failed")
  assert.equal(calls, 0)
  tracker.dispose()
})

check("tracker：超时要归成 timed out，不是把原始错误喷给用户", async () => {
  // AbortSignal.timeout 的 timer 是 unref 的：如果测试里没有别的活儿，事件循环
  // 会在超时触发之前就空掉，node 直接判定「Promise 还没 resolve 循环已经空了」。
  // 先用一个 ref 的 timer 托住循环，再让 tracker 去超时。
  const hold = new Promise((resolve) => setTimeout(resolve, 80))
  const tracker = createUpdateTracker({
    readVersion: () => "1.0.0",
    timeoutMs: 10,
    fetchTags: ({ signal }) =>
      new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })))
      }),
  })
  await tracker.check()
  await hold
  assert.equal(tracker.state.state, "failed")
  assert.equal(tracker.state.error, "timed out")
  tracker.dispose()
})

check("tracker：schedule(false) 真的把 timer 停掉", async () => {
  const tracker = createUpdateTracker({ readVersion: () => "1.0.0", fetchTags: async () => [] })
  tracker.schedule(true)
  tracker.schedule(false)
  assert.equal(tracker.state.state, "off")
  const before = tracker.state.state
  await new Promise((resolve) => setTimeout(resolve, 30))
  assert.equal(tracker.state.state, before, "关掉之后不该还有检查在跑")
  tracker.dispose()
})

check("tracker：schedule(false) 之后 schedule(true) 能恢复", async () => {
  const tracker = createUpdateTracker({ readVersion: () => "1.0.0", fetchTags: async () => ["v1.0.0"], delayMs: 0 })
  tracker.schedule(true)
  tracker.schedule(false)
  assert.equal(tracker.state.state, "off")
  tracker.schedule(true)
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.equal(tracker.state.state, "current")
  tracker.dispose()
})

check("tracker：重复 schedule(true) 不叠加 timer", async () => {
  let calls = 0
  const tracker = createUpdateTracker({
    readVersion: () => "1.0.0",
    delayMs: 5,
    fetchTags: async () => {
      calls += 1
      return ["v1.0.0"]
    },
  })
  tracker.schedule(true)
  tracker.schedule(true)
  tracker.schedule(true)
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.equal(calls, 1, "三次排期只能落一次检查")
  tracker.dispose()
})

check("tracker：dispose 之后不再发请求", async () => {
  let calls = 0
  const tracker = createUpdateTracker({
    readVersion: () => "1.0.0",
    delayMs: 0,
    fetchTags: async () => {
      calls += 1
      return ["v1.0.0"]
    },
  })
  tracker.schedule(true)
  tracker.dispose()
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.equal(calls, 0)
})

check("tracker：document 把已经装上的 available 折成 current", async () => {
  let version = "1.0.0"
  const tracker = createUpdateTracker({ readVersion: () => version, fetchTags: async () => ["v1.1.0"] })
  await tracker.check()
  let doc = tracker.document({ python: { wanted: "1.0.0", installed: "1.0.0", venv: true } })
  assert.equal(doc.update.state, "available")
  assert.equal(doc.update.spec, `git+https://github.com/${UPDATE_REPOSITORY}.git#v1.1.0`)
  assert.equal(doc.version, "1.0.0")
  // 装上之后新实例读到的就是新版本
  version = "1.1.0"
  doc = tracker.document()
  assert.equal(doc.update.state, "current", "装完不该还提示有更新")
  assert.equal(doc.update.spec, undefined, "current 时不给出安装 spec")
  tracker.dispose()
})

check("tracker：stale 的 available 在装完之后也要清掉", async () => {
  let version = "1.0.0"
  const answers = [["v1.1.0"], new Error("boom")]
  const tracker = createUpdateTracker({
    readVersion: () => version,
    fetchTags: async () => {
      const next = answers.shift()
      if (next instanceof Error) throw next
      return next
    },
  })
  await tracker.check()
  await tracker.check()
  assert.equal(tracker.state.stale, "available")
  version = "1.1.0"
  const doc = tracker.document()
  assert.equal(doc.update.stale, undefined)
  assert.equal(doc.update.state, "failed", "状态仍是上次的失败，但不再是「有个装着的新版本」")
  tracker.dispose()
})

// ── 接线层 ─────────────────────────────────────────────────────────────────

/** 一个够用的假 ctx：记下注册的工具、inject、effect 与事件监听。 */
function fakeCtx() {
  const tools = []
  const injected = []
  const handlers = new Map()
  const disposers = []
  const effects = []
  const ctx = {
    logger: { info: () => {} },
    tools: { register: (tool) => tools.push(tool) },
    inject(deps, callback) {
      injected.push([...deps])
      // 模拟「服务在」：立刻把子上下文交给回调。
      if (deps.includes("pluginManager")) {
        callback({
          pluginManager: ctx._manager,
          effect: (fn, label) => {
            effects.push(label)
            const dispose = fn()
            if (typeof dispose === "function") disposers.push(dispose)
          },
        })
      }
    },
    on(event, fn) {
      const list = handlers.get(event) ?? []
      list.push(fn)
      handlers.set(event, list)
    },
    effect(fn, label) {
      effects.push(label)
      const dispose = fn()
      if (typeof dispose === "function") disposers.push(dispose)
    },
    _tools: tools,
    _injected: injected,
    _handlers: handlers,
    _effects: effects,
    _disposeAll() {
      for (const dispose of disposers.splice(0)) dispose()
    },
    _manager: undefined,
  }
  return ctx
}

check("index：只 inject tools，插件管理器按需注入", async () => {
  const plugin = await import("../plugin/index.mjs")
  assert.equal(plugin.name, "ui-locate-update")
  assert.deepEqual(plugin.inject, ["tools"], "pluginManager 不能写死，否则没它的部署起不来")
})

check("index：Config 两个开关默认都开，且是 volatile", async () => {
  const plugin = await import("../plugin/index.mjs")
  const parsed = plugin.Config({})
  assert.equal(parsed.updateCheck.get(), true)
  assert.equal(parsed.pythonSync.get(), true)
  // 热改：改完再读就是新值
  parsed.updateCheck.set(false)
  assert.equal(parsed.updateCheck.get(), false)
})

check("index：apply 注册了 ui_locate_update 并声明输出形状", async () => {
  const plugin = await import("../plugin/index.mjs")
  const ctx = fakeCtx()
  plugin.apply(ctx, plugin.Config({}))
  try {
    const tool = ctx._tools.find((item) => item.name === "ui_locate_update")
    assert.ok(tool, "工具必须注册")
    assert.match(tool.description, /check/)
    assert.match(tool.description, /apply/)
    const schema = tool.output.schema
    for (const field of ["version", "repository", "home", "update", "python"]) {
      assert.ok(schema.properties[field], `输出必须有 ${field}`)
    }
    assert.deepEqual(schema.properties.version.required, true)
    // presentCall 要给得出东西，否则 UI 会空
    const presented = tool.presentCall({ action: "check" })
    assert.equal(presented.title, "ui_locate_update")
    assert.equal(presented.card, "generic")
    assert.deepEqual(presented.rawInput, { action: "check" })
  } finally {
    ctx._disposeAll()
  }
})

check("index：apply 挂了 pluginManager 的按需注入与 volatile-update 监听", async () => {
  const plugin = await import("../plugin/index.mjs")
  const ctx = fakeCtx()
  plugin.apply(ctx, plugin.Config({}))
  try {
    assert.deepEqual(ctx._injected[0], ["pluginManager"])
    assert.ok(ctx._handlers.get("loader/volatile-update")?.length, "配置热改要能被接住")
    assert.ok(ctx._effects.includes("ui-locate: update timers"))
    assert.ok(ctx._effects.includes("ui-locate: plugin manager"))
  } finally {
    ctx._disposeAll()
  }
})

check("index：工具 action 非法时不装、也不崩", async () => {
  const plugin = await import("../plugin/index.mjs")
  const ctx = fakeCtx()
  plugin.apply(ctx, plugin.Config({ updateCheck: false }))
  try {
    const tool = ctx._tools.find((item) => item.name === "ui_locate_update")
    const value = await tool.execute({ action: "delete-everything" })
    assert.equal(value.result.ok, false)
    assert.match(value.result.error, /unknown action/)
    // updateCheck 关掉时状态就是 off，不该偷偷联网
    assert.equal(value.update.state, "off")
    assert.equal(typeof value.version, "string")
  } finally {
    ctx._disposeAll()
  }
})

check("index：check 在 updateCheck 关掉时不联网", async () => {
  const plugin = await import("../plugin/index.mjs")
  const ctx = fakeCtx()
  plugin.apply(ctx, plugin.Config({ updateCheck: false }))
  try {
    const tool = ctx._tools.find((item) => item.name === "ui_locate_update")
    const value = await tool.execute({ action: "check" })
    assert.equal(value.result.ok, true)
    assert.equal(value.update.state, "off")
  } finally {
    ctx._disposeAll()
  }
})

check("index：apply 时 python 一栏反映真实 venv 状态", async () => {
  const plugin = await import("../plugin/index.mjs")
  const ctx = fakeCtx()
  plugin.apply(ctx, plugin.Config({ updateCheck: false }))
  try {
    const tool = ctx._tools.find((item) => item.name === "ui_locate_update")
    const value = await tool.execute({ action: "check" })
    assert.equal(typeof value.home, "string")
    assert.ok(value.home.endsWith("ui-locate"), value.home)
    assert.equal(value.python.wanted, packageVersion())
    // 这台机器的 venv 可能还没建；两种都算对，但不能是 undefined 之外的糊状。
    assert.equal(typeof value.python.venv, "boolean")
    assert.ok(value.python.installed === undefined || typeof value.python.installed === "string")
    assert.equal(value.repository, UPDATE_REPOSITORY)
  } finally {
    ctx._disposeAll()
  }
})

/**
 * 递归找值为 `undefined` 的键。
 *
 * DSH 按「无损 JSON」校验工具返回值，`undefined` 不是合法 JSON 值：只要有一处
 * 漏出来，整个返回值就被判成 `INVALID_TOOL_OUTPUT`，调用直接失败。这个断言就是
 * 把那条规则搬进测试，免得又一次在装进 DSH 之后才发现。
 */
function undefinedPaths(value, path = "") {
  if (Array.isArray(value)) {
    return value.flatMap((item, index) =>
      item === undefined ? [`${path}[${index}]`] : undefinedPaths(item, `${path}[${index}]`),
    )
  }
  if (value === null || typeof value !== "object") return []
  const found = []
  for (const [key, item] of Object.entries(value)) {
    const here = path === "" ? key : `${path}.${key}`
    if (item === undefined) found.push(here)
    else found.push(...undefinedPaths(item, here))
  }
  return found
}

check("index：venv 没建时返回值里也不能有 undefined", async () => {
  const plugin = await import("../plugin/index.mjs")
  const ctx = fakeCtx()
  // 指到一个不存在的目录，逼出「版本读不到」的路径——那正是 undefined 的来源。
  const before = process.env.UI_LOCATE_HOME
  process.env.UI_LOCATE_HOME = join(tmpdir(), "ui-locate-不存在-" + Date.now())
  try {
    plugin.apply(ctx, plugin.Config({ updateCheck: false, pythonSync: false }))
    const tool = ctx._tools.find((item) => item.name === "ui_locate_update")
    for (const action of ["check", "apply", "bogus"]) {
      const value = await tool.execute({ action })
      assert.deepEqual(undefinedPaths(value), [], `action=${action} 的返回值里有 undefined`)
    }
  } finally {
    if (before === undefined) delete process.env.UI_LOCATE_HOME
    else process.env.UI_LOCATE_HOME = before
    ctx._disposeAll()
  }
})

check("index：apply 在没有 pluginManager 时也能起来", async () => {
  const plugin = await import("../plugin/index.mjs")
  const ctx = fakeCtx()
  // 让 inject 回调永远不来，模拟没有插件管理器的部署
  ctx.inject = (deps) => {
    ctx._injected.push([...deps])
  }
  plugin.apply(ctx, plugin.Config({ updateCheck: false }))
  try {
    const tool = ctx._tools.find((item) => item.name === "ui_locate_update")
    const value = await tool.execute({ action: "apply" })
    assert.equal(value.result.ok, false)
    assert.match(value.result.error, /plugin manager unavailable/)
  } finally {
    ctx._disposeAll()
  }
})

check("index：apply 在拿到 pluginManager 后会把 spec 交出去", async () => {
  const plugin = await import("../plugin/index.mjs")
  const installed = []
  const ctx = fakeCtx()
  ctx._manager = {
    async installBundle(spec, options) {
      installed.push({ spec, options })
      return { ok: true, value: { application: "restart-required", version: "9.9.9" } }
    },
  }
  // 让 tracker 查到一个比当前高的版本，且不碰网络
  const realFetch = globalThis.fetch
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => [{ name: "v999.0.0" }] })
  try {
    plugin.apply(ctx, plugin.Config({ pythonSync: false }))
    const tool = ctx._tools.find((item) => item.name === "ui_locate_update")
    const value = await tool.execute({ action: "apply" })
    assert.equal(value.result.ok, true)
    assert.equal(installed.length, 1)
    assert.match(installed[0].spec, /^git\+https:\/\/github\.com\/citydirector\/ui-locate-mcp\.git#v999\.0\.0$/)
    assert.ok(installed[0].options.requestId, "要给 requestId，否则取消不了")
    assert.match(value.result.note, /Python/)
    ctx._disposeAll()
  } finally {
    globalThis.fetch = realFetch
  }
})

// ── 收尾 ───────────────────────────────────────────────────────────────────

test("全部用例已跑完", () => {
  assert.equal(failed, 0, `失败 ${failed} 条`)
  assert.ok(passed > 0, "一条都没跑起来")
})
