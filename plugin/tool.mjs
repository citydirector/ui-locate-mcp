/**
 * 一个不依赖 `@deepseek-ai/dsh-tools` 的最小 `defineTool`。
 *
 * ## 为什么不能直接 import 官方的那个
 *
 * 官方 `defineTool` 住在 `@deepseek-ai/dsh-tools` 里，而**这个包不在插件
 * 能解析到的位置**：DSH 只往 `profiles/<名字>/node_modules/@deepseek-ai/`
 * 下放 `cosmokit` 与 `schemastery` 两个包，`dsh-tools` 在宿主自己的
 * `app.asar` 里。插件里写 `import { defineTool } from "@deepseek-ai/dsh-tools"`
 * 会在装上的那一刻抛 `ERR_MODULE_NOT_FOUND`，整个插件起不来。
 *
 * 这台机器上真正在跑的 `dsh-ops` 就是反证：它只声明 `ctx.tools.register()`，
 * 从不 import 这个包。本文件照同样的路子走。
 *
 * ## 那还剩下什么要自己做
 *
 * `ctx.tools.register()` 只检查三件事：`output` 的形状、`output.schema` 是否
 * 落在受支持的 JSON Schema 子集里、`timeoutMs` 是否为正。**它不会校验参数**
 * ——官方 `defineTool` 的价值有一半在于把 `execute` 包了一层参数校验，裸注册
 * 没有这层。所以这里把关键几条补上：
 *
 * - `timeoutMs` 正有限数；
 * - `output` 必须是 `{ schema, render }`；
 * - `output.schema` 里每个 `object` 节点都必须显式写 `additionalProperties`
 *   （宿主编译器会直接拒掉漏写的，见 README「必须知道的坑」第 16 条）；
 * - `parameters` 必须是 object 根；
 * - `execute` 调用前按 `parameters` 校验一遍实参。
 *
 * 校验器的能力故意收得很窄：只认这个插件真正用到的几种形状（string /
 * number / boolean / object / array）。宿主那边的完整实现支持更多关键字，
 * 但这里多写一行就等于多一处会和上游走偏的代码。
 *
 * @module ui-locate-mcp/tool
 */

/** 参数校验只认这几种类型。 */
const TYPE_CHECKS = {
  string: (value) => typeof value === "string",
  number: (value) => typeof value === "number" && Number.isFinite(value),
  boolean: (value) => typeof value === "boolean",
  object: (value) => value !== null && typeof value === "object" && !Array.isArray(value),
  array: (value) => Array.isArray(value),
}

/** 实参不合法时抛这个，形状对齐宿主的 `ToolArgsError`。 */
export class ToolArgsError extends Error {
  constructor(violations) {
    super(`invalid tool arguments: ${violations.join("; ")}`)
    this.name = "ToolArgsError"
    this.violations = violations
  }
}

/**
 * 递归核对每个 `object` 节点都写了 `additionalProperties`。
 *
 * 这条是宿主编译器的硬要求，漏写会让整个工具定义被拒——与其等装上之后
 * 才在宿主日志里看见，不如在这里当场抛。
 *
 * @param node - 当前 schema 节点。
 * @param path - 出错信息里用的位置。
 * @param violations - 收集结果的数组。
 */
function checkObjectNodes(node, path, violations) {
  if (node === null || typeof node !== "object") return
  if (node.type === "object" && typeof node.additionalProperties !== "boolean") {
    violations.push(`${path}.additionalProperties must be explicitly true or false`)
  }
  for (const [key, child] of Object.entries(node.properties ?? {})) {
    checkObjectNodes(child, `${path}.properties.${key}`, violations)
  }
  if (node.items) checkObjectNodes(node.items, `${path}.items`, violations)
  for (const [index, branch] of (node.oneOf ?? []).entries()) {
    checkObjectNodes(branch, `${path}.oneOf[${index}]`, violations)
  }
}

/**
 * 按 `parameters` 校验一次实参。
 *
 * 只做「类型对不对」和「必填有没有」，不做转换——模型传错类型时直接报错
 * 比悄悄转成字符串更好查。
 *
 * @param schema - object 根的 JSON Schema。
 * @param args - 模型给的实参。
 * @returns 违规描述列表，空数组表示通过。
 */
export function validateArgs(schema, args) {
  const violations = []
  if (args !== undefined && (args === null || typeof args !== "object" || Array.isArray(args))) {
    violations.push(`arguments must be an object, got ${JSON.stringify(args)}`)
    return violations
  }
  const provided = args ?? {}
  const properties = schema?.properties ?? {}
  for (const [key, value] of Object.entries(provided)) {
    if (value === undefined) continue
    const declared = properties[key]
    if (declared === undefined) {
      // 与宿主一致的取舍：多出来的键不拒（JSON Schema 默认如此），但要能看出来。
      continue
    }
    const check = TYPE_CHECKS[declared.type]
    if (check !== undefined && !check(value)) violations.push(`"${key}" must be a ${declared.type}`)
  }
  for (const key of schema?.required ?? []) {
    if (provided[key] === undefined) violations.push(`"${key}" is required`)
  }
  return violations
}

/**
 * 定义并返回一个可以直接交给 `ctx.tools.register()` 的工具。
 *
 * 返回的形状就是宿主注册接口要的那几个字段，不多不少：多出来的键宿主管不着，
 * 少一个就起不来。
 *
 * @param options - `{ name, description, parameters, output, timeoutMs?, execute }`。
 *   `parameters` 是完整的 object 根 JSON Schema。
 * @returns 注册用的工具定义。
 */
export function defineTool(options) {
  const name = options?.name
  if (typeof name !== "string" || name === "") throw new TypeError("defineTool: name is required")

  if (options.timeoutMs !== undefined && !(Number.isFinite(options.timeoutMs) && options.timeoutMs > 0)) {
    throw new TypeError(`defineTool(${name}): timeoutMs must be a positive finite number`)
  }

  const output = options.output
  if (output === null || typeof output !== "object" || typeof output.render !== "function") {
    throw new TypeError(`tool "${name}" must declare output { schema, render }`)
  }
  const schemaViolations = []
  checkObjectNodes(output.schema, "schema", schemaViolations)
  if (schemaViolations.length > 0) throw new TypeError(`unsupported JSON schema: ${schemaViolations.join("; ")}`)

  const parameters = options.parameters ?? { type: "object", properties: {} }
  if (parameters.type !== "object") throw new TypeError(`tool "${name}": parameters must be an object schema`)

  const execute = options.execute
  if (typeof execute !== "function") throw new TypeError(`tool "${name}": execute is required`)

  const tool = {
    name,
    description: options.description ?? "",
    parameters,
    output: { schema: output.schema, render: output.render },
    async execute(args, exec) {
      const violations = validateArgs(parameters, args)
      if (violations.length > 0) throw new ToolArgsError(violations)
      return execute(args, exec)
    },
  }
  if (options.timeoutMs !== undefined) tool.timeoutMs = options.timeoutMs
  if (typeof options.presentCall === "function") tool.presentCall = options.presentCall
  return tool
}
