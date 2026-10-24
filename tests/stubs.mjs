/**
 * 在跑测试前把最小的 `@deepseek-ai/schemastery` 桩装进 `node_modules/`。
 *
 * ## 为什么需要桩
 *
 * `plugin/index.mjs` 依赖 `@deepseek-ai/schemastery` 来声明 Config。这个包由
 * DSH Host 提供（profile 的 `node_modules/@deepseek-ai/` 下就装着它），但本
 * 仓库不安装它——测试要**真的 import 那个文件**，所以得有人把这个名字解析掉。
 *
 * 桩只复刻被测代码实际用到的形状：`z.object({...})` 给出一个能调用、也能
 * 直接读字段的对象，字段带 `.get()`；`z.boolean()` 给出可链式调
 * `.default()` / `.volatile()` / `.description()` 的字段规格。真的 schema
 * 校验、类型推导一律不做——那正是要留给 DSH 自己的。
 *
 * ## 为什么不再给 `@deepseek-ai/dsh-tools` 造桩
 *
 * 那个桩曾经在这里，而且**它复刻的规则救过一次命**（漏写
 * `additionalProperties` 会让插件装不上）。但复刻的规则终究是抄来的，抄漏
 * 一条就等于白测。现在 `defineTool` 换成了本仓库自己的 `plugin/tool.mjs`——
 * 它带着同一条检查，而且能被直接测，不需要桩。
 *
 * ## 为什么不在仓库里放 node_modules
 *
 * 桩是测试脚手架，不是依赖。提交进去以后 `node_modules` 就得长期出现在版本
 * 控制里，还会让人误以为本包真的依赖它。所以改成「跑测试时现造」：
 * `node_modules/` 进 `.gitignore`，干净 checkout 直接 `node --test tests/`
 * 也能跑。
 *
 * @module ui-locate-mcp/tests/stubs
 */

import { existsSync, mkdirSync, writeFileSync } from "node:fs"
import { join } from "node:path"

const SCHEMASTERY_PKG = {
  name: "@deepseek-ai/schemastery",
  version: "3.18.5",
  type: "module",
  main: "index.mjs",
  exports: { ".": "./index.mjs" },
}

const SCHEMASTERY_SRC = `/**
 * schemastery 的最小桩：只实现本包用到的形状。
 *
 * 真的 schemastery 会把 z.object({...}) 变成一个「可以调用、也能直接读字段」
 * 的东西——字段本身是带 .get() 的 volatile 引用。这里复刻的正是这两个性质，
 * 因为 apply() 依赖它们：config.updateCheck.get() 拿当前值，Config({}) 能拿
 * 默认值建一份新实例。
 */

function field(type, fallback) {
  const spec = {
    type,
    fallback,
    volatile: () => spec,
    description: () => spec,
    default: (value) => ((spec.fallback = value), spec),
  }
  return spec
}

const z = {
  boolean: () => field("boolean", undefined),
  string: () => field("string", undefined),
  number: () => field("number", undefined),
  object(shape) {
    const build = (input = {}) => {
      const instance = {}
      for (const [key, spec] of Object.entries(shape)) {
        let value = key in input ? input[key] : spec.fallback
        instance[key] = { get: () => value, set: (next) => ((value = next), next) }
      }
      return instance
    }
    build.__shape = shape
    return build
  },
}

export default z
export { z }
`

/** 写一个桩包；已存在就不覆盖（免得每次跑测试都重写文件）。 */
function install(root, name, pkg, source) {
  const dir = join(root, "node_modules", ...name.split("/"))
  const entry = join(dir, "index.mjs")
  if (existsSync(entry)) return false
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, "package.json"), `${JSON.stringify(pkg, null, 2)}\n`)
  writeFileSync(entry, source)
  return true
}

/**
 * 确保桩在。
 *
 * @param root - 包根目录。
 * @returns 本次实际新建的包名。
 */
export function ensureStubs(root) {
  const created = []
  if (install(root, "@deepseek-ai/schemastery", SCHEMASTERY_PKG, SCHEMASTERY_SRC)) {
    created.push("@deepseek-ai/schemastery")
  }
  return created
}
