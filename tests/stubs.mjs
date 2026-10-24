/**
 * 在跑测试前把两个最小桩装进 `node_modules/`。
 *
 * ## 为什么要桩
 *
 * `plugin/index.mjs` 依赖 `@deepseek-ai/schemastery`（Config 的 volatile 引用）
 * 和 `@deepseek-ai/dsh-tools`（`defineTool`）。这两个包由 DSH Host 在运行时
 * 提供，本仓库不安装它们——但测试要**真的 import 那个文件**，所以得有人把
 * 这两个名字解析掉。
 *
 * 桩只复刻被测代码实际用到的形状：`z.object({...})` 给出一个能调用、也能
 * 直接读字段的对象，字段带 `.get()`；`defineTool` 原样返回 options。真的
 * schema 校验、类型推导一律不做——那正是要留给 DSH 自己的。
 *
 * ## 为什么不在仓库里放 node_modules
 *
 * 桩是测试脚手架，不是依赖。提交进去以后 `node_modules` 就得长期出现在
 * 版本控制里，而且会让人误以为本包真的依赖这两个东西。所以改成「跑测试时
 * 现造」：`node_modules/` 进 `.gitignore`，干净 checkout 直接 `node --test
 * tests/` 也能跑。
 *
 * @param root - 包根目录。
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

const TOOLS_PKG = {
  name: "@deepseek-ai/dsh-tools",
  version: "0.2.1-rc.2",
  type: "module",
  main: "index.mjs",
  exports: { ".": "./index.mjs" },
}

// 桩源码用数组拼，避免在模板字符串里再嵌一层反引号与 ${}。
const TOOLS_SRC =
  [
    "/**",
    " * dsh-tools 的最小桩。",
    " *",
    " * 真实现会把 parameters / output.schema 编译成受约束的 JSON Schema，再包一层",
    " * 参数与输出的校验。测试要从外面拿到工具定义去调用 execute，所以这里直接返回",
    " * options —— 但**两条真实规则必须复刻**，否则本仓库会漏掉真实的失败：",
    " *",
    " *   1. 每个 object 节点都要显式写 additionalProperties（true 或 false），漏写",
    " *      真实现直接抛错、整个插件起不来。",
    " *   2. timeoutMs 必须是正有限数。",
    " *",
    " * 这两条都是实际踩到过的（见 README 的「必须知道的坑」），只靠最小桩会一路绿",
    " * 到装进 DSH 才炸。",
    " */",
    "",
    "/** 递归核对 schema 里每个 object 节点都写了 additionalProperties。 */",
    "function checkSchema(node, path) {",
    "  if (!node || typeof node !== \"object\" || Array.isArray(node)) return",
    "  if (node.type === \"object\") {",
    "    if (typeof node.additionalProperties !== \"boolean\") {",
    "      throw new TypeError(",
    "        `unsupported JSON schema: ${path}.additionalProperties must be explicitly true or false`,",
    "      )",
    "    }",
    "    for (const [key, child] of Object.entries(node.properties ?? {})) {",
    "      checkSchema(child, `${path}.properties.${key}`)",
    "    }",
    "  }",
    "  if (node.type === \"array\" && node.items) checkSchema(node.items, `${path}.items`)",
    "  for (const [index, branch] of (node.oneOf ?? []).entries()) {",
    "    checkSchema(branch, `${path}.oneOf[${index}]`)",
    "  }",
    "}",
    "",
    "export function defineTool(options) {",
    "  if (options.timeoutMs !== undefined && !(Number.isFinite(options.timeoutMs) && options.timeoutMs > 0)) {",
    "    throw new TypeError(\"timeoutMs must be a positive finite number\")",
    "  }",
    "  checkSchema(options.output?.schema, \"schema\")",
    "  return { ...options, __tool: true }",
    "}",
    "",
  ].join("\n")

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
 * 确保两个桩都在。
 *
 * @param root - 包根目录。
 * @returns 本次实际新建的包名。
 */
export function ensureStubs(root) {
  const created = []
  if (install(root, "@deepseek-ai/schemastery", SCHEMASTERY_PKG, SCHEMASTERY_SRC)) {
    created.push("@deepseek-ai/schemastery")
  }
  if (install(root, "@deepseek-ai/dsh-tools", TOOLS_PKG, TOOLS_SRC)) {
    created.push("@deepseek-ai/dsh-tools")
  }
  return created
}
