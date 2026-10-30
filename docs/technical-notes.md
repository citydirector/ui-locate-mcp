# 实验记录与实现笔记

[返回 README](../README.md)

以下是历史实验记录，保留算法取舍、测量和排障过程。性能数字来自当时的设备、依赖和截图，不代表其他机器的速度；安装、更新和环境要求以当前 README 与源码为准。

## 实测性能（8GB 显存笔记本独显，1567×881 截图）

三种后端跑同一个 640×640 检测（20 次取中位）：

| 后端 | 中位 | 建会话 | 备注 |
|------|------|--------|------|
| **DirectML** | **10.1 ms** | 1.5 s | 走独显；AMD / Intel 显卡同样可用 |
| CUDA | 10.8 ms | 35 s（首次） | 需要 `-Cuda` 安装，1.8 GB |
| CPU | 176 ms | 0.2 s | 兜底 |

DirectML 与 CPU 的输出最大绝对误差 5.8e-4（值域 0~654），可忽略。

| 模式 | 检测 | OCR | 合计 |
|------|------|-----|------|
| 单块 + OCR | 15.8 ms | 424 ms | **440 ms** |
| 分块(960) + OCR | 60 ms | 420 ms | 480 ms |
| 单块，无 OCR | 15.8 ms | — | **16 ms** |

1920×1080 真实桌面（130 个元素左右），两种 OCR 路线差距明显：

| OCR 路线 | 检测 | OCR | 合计 | 检出 |
|----------|------|-----|------|------|
| `full` 整页 | 283 ms | 2083 ms | **2398 ms** | 125 |
| `mosaic` 裁片拼图 | 214 ms | 596 ms | **828 ms** | 73 |

两块屏一起扫（一块 2560×1440 + 一块 1920×1080，按屏分别切块）：

| 屏 | 块数 | 检测 | 元素 |
|------|------|------|------|
| 主屏 2560×1440 | 12 | 323 ms | 26 |
| 副屏 1920×1080 | 6 | 145 ms | 60 |
| 合计（`monitor=all`） | 18 | 468 ms | 86~112 |

- 单块检测 15.8 ms 是纯 GPU 极限（模型输入 640×640）；DirectML 上同样输入是 10.1 ms。
- 分块模式把小图标找回（单块检出 21 个，分块检出 23 个），代价约 45 ms。
- OCR 是瓶颈且**必须走 CPU**：PP-OCR 模型太小，CUDA 上小图搬运开销
  反而慢 5 倍（2135 ms vs 442 ms）。
- 智能分割（默认开）在全屏真实桌面上多花约 300 ms（Canny + 密度统计），
  换来切缝不劈开元素、空白块不送推理。

### 两种 OCR 路线怎么选

`text_mode=mosaic`（**默认**）：只把检测框里的裁片拼成一张 1200 宽的图再 OCR，
1920×1080 实测 **530~600 ms**，比 full 快 4 倍。代价是检测器没框到的文字就丢了
（正文段落基本认不出，按钮 / 菜单 / 图标旁边的标签没问题）。
适合「我知道目标是个控件」的场景——游戏 UI、工具栏、对话框。

`text_mode=full`：整页只跑一次 OCR，召回最高（连正文段落都认），
适合「我要找的东西可能在任意位置」。1920×1080 约 2.1 秒。

⚠ 别试图分页来进一步加速：实测 6 页 4948 ms、4 页 2921 ms，
每页都要付一次 OCR 的固定开销，页数越多越慢。`paginate()` 保留作对照。

## 必须知道的坑

1. **CUDA 下每个新进程的首次推理约 35 秒**。cuDNN 要为 113 个卷积层逐一枚举算法，
   换 `EXHAUSTIVE` / `HEURISTIC`、开关 `cudnn_conv_use_max_workspace` 都绕不过。
   唯一解法是**常驻服务**：预热一次，进程活着就一直快。DirectML 没有这个问题
   （建会话约 1.5 秒），所以默认后端选 DirectML 的机器上手体验更好。
2. **必须同时设 `os.add_dll_directory()` 和 `os.environ["PATH"]`**。ORT 的
   provider bridge 用 ALTERED_SEARCH_PATH 加载 CUDA dll，只加前者找不到依赖。
3. **`rapidocr-onnxruntime` 会把 `onnxruntime-gpu` 换成 CPU 版**。装完要
   `uv sync --reinstall-package onnxruntime-gpu` 修回来。装包后 `cv2` 目录
   也可能丢失，用 `uv sync --reinstall-package opencv-python-headless` 修复。
4. **某些安全软件会拦住「模型目录下 venv 的 python 写文件」**。实测过这种情况：
   同一台机器上原始解释器能写、venv 里的 python 报 `PermissionError`。量化产物
   一律落到工程 `.cache/`，别往模型目录写。
5. **INT8 动态量化对 YOLO 无效**：生成 `ConvInteger(10)`，CPU / CUDA EP 都不实现。
   要做量化必须用带校准集的静态 QDQ。
6. **bash 里变量展开失效**（本机 shell 特性），命令里一律写绝对路径。
7. **别给 mosaic 分页**。每页都要付一次 OCR 固定开销，页数越多越慢
   （实测 1 页 579 ms，6 页 4948 ms）。要快就把整张拼图压到一个 1200 宽的页面里。
8. **输入注入成不生效，取决于前台是谁，不是取决于本进程的权限**。桌面程序在
   前台时光标移动正常（实测 `cursor_moved: true`）；全屏游戏捕获鼠标时
   `SetCursorPos` 与 `SendInput` 的移动全部失败（返回成功、错误码含糊、光标
   一步不动）。**别看「读回光标」判断注入有没有成功** —— 被捕获时这个值本
   来就不变，判据本身失效。要判成没成，比截图。这也是为什么 `/act` 的
   `verified` 要和 `ok` 分开看，且抓屏失败时它给 `null` 而不是 `false`。
9. **`.python-version` 必须是 3.12**。uv 默认拿 3.14，而 `onnxruntime-gpu==1.23.2`
   只发到 cp313，装不上。
10. **`/detect` 的参数要同时支持 query 和 body**。MCP 层习惯把参数放 JSON body，
    而早期实现只读 query，导致 `text_mode`、`conf` 全被静默忽略——症状是
    「参数改了没反应」，很难查。
11. **别用 `python.exe` 起常驻服务，要用 `pythonw.exe`**。`python.exe` 是 Console
    子系统程序，Windows 启动时**必然分配控制台**，`-WindowStyle Hidden` /
    `CREATE_NO_WINDOW` 只是把窗口藏起来，`conhost.exe` 照样挂在进程上——
    结果就是一个常驻的黑窗口。`pythonw.exe` 是 Windows 子系统，压根不分配控制台。
12. **PIL `ImageGrab.grab(bbox=...)` 对负坐标副屏返回全黑**。必须
    `all_screens=True` 抓整张虚拟桌面再用 numpy 切片，别用 bbox 直接抓。
13. **别对整块虚拟桌面切块**。虚拟桌面的外接矩形往往远大于各屏实际像素之和 ——
    实测过一个双屏布局，外接矩形 1184 万像素，两块屏合计只有 726 万，
    **38.7% 是显示器之间根本不存在的空白**。要按屏分别规划网格，每块屏从自己的
    (0,0) 起算。
14. **判断切缝位置要用对齐后的 `xb[i]`，不能用 tile 的 `x0`**。`x0 = xb[i] - ox`
    带着重叠量，用它判断「切缝是否穿过元素」会得出错误结论。
15. **OCR 会把按钮图标认成字符**。「插件」常被读成 `8 插件` / `$ 插件` / `④插件`，
    导致「完全相等」匹配永远不成立，精确查询降级成模糊查询就点偏了。
    `ocr_match._strip_icon_noise()` 在匹配前剥掉首尾噪声字符（只剥首尾不动中间，
    剥空则退回原串）。
16. **工具返回的 object 必须显式写 `additionalProperties`**。DSH 的 schema 编译器
    对每个 object 节点都要求 `additionalProperties: true` 或 `false`，漏写会抛
    `unsupported JSON schema: ... must be explicitly true or false`，整个插件起不来。
    顶层不例外，嵌套的 `update` / `python` 也一样。
17. **工具返回值里不能有 `undefined`**。校验器按「无损 JSON」判，`undefined` 不是
    合法 JSON 值——一处漏出来整个返回值就被判成 `INVALID_TOOL_OUTPUT`，调用直接
    失败。而这个插件返回的到处是「可能读不到」的字段（版本号、venv 里的包版本、
    上次查到的最新标签、安装结果里的 `application`）。出口统一过一遍
    `lossless()`，别指望每处赋值都记得判断。
18. **`node_modules` 里的 `@deepseek-ai` 桩不能当成真包**。本仓库测试用现场生成的
    最小桩（`tests/stubs.mjs`），它能验证自己写的逻辑，但**验不了 DSH 的 schema
    规则**——真 `defineTool` 有几十行约束是桩复刻不出来的。想验真实约束，从
    `app.asar` 里解出真包再跑（见 `experiments/`）。
19. **插件里不要 import `@deepseek-ai/dsh-tools`**。这个包在宿主自己的 `app.asar`
    里，而 DSH 只往 `profiles/<名字>/node_modules/@deepseek-ai/` 下放 `cosmokit`
    与 `schemastery` 两个包。插件里写这句 import，装上那一刻就
    `ERR_MODULE_NOT_FOUND`，整个插件起不来（实测：把同一份文件放进一个只有这两个
    包的 `node_modules` 里，报错正是这句）。要用工具就用自带的 `plugin/tool.mjs`，
    或者像 `dsh-ops` 那样直接 `ctx.tools.register()`。

20. **DirectML 会因为 `allowzero=1` 整图编译失败，然后静默回退 CPU**。
    Ultralytics 导出的 YOLO 里 8 个 `Reshape` 节点都带 `allowzero = 1`，而
    DirectML EP 不支持这个 opset 14 属性。表现极具迷惑性：Python 侧只看到
    `EP Error 'utf-8' codec can't decode byte 0xb2 ...`（真实错误是 UTF-16LE
    的 `80070057 参数错误` 被当 UTF-8 读），而且**不抛异常**，只是
    `get_providers()` 悄悄变成 `CPUExecutionProvider`。
    所以判断 DML 有没有生效**只能看 `sess.get_providers()[0]`**，看有没有报错是不够的。

    修法在 `src/ui_locate/dml_fix.py`：直接改模型字节，把 `allowzero = 1` 换成
    `0`。该模型所有 shape 常量都不含 `0`，两种语义完全等价，所以改是安全的
    （代码里会先检查，含 `0` 就抛 `UnsafeModel` 拒绝改）。补丁在**内存里**完成，
    不落盘，也不引入 `onnx` 依赖（手写最小 protobuf 遍历）。

    验证：改完 `实跑 provider = DmlExecutionProvider`，中位 10.1 ms；与 CPU 相比
    最大绝对误差 5.8e-4（值域 0~654）。三个 ORT 版本（1.22 / 1.23 / 1.24）都有
    这个问题，不是版本回归。

21. **`installBundle` 的返回形态有两套，只认一套会把每次成功都报成失败**。
    （历史记录：0.5.x 自研更新器用过这段逻辑，0.6.0 已随更新器一起删除。
    留在这里是因为只要将来再有人碰 `pluginManager`，这个坑一模一样。）

    | 拿到 manager 的方式 | 返回值 |
    |---|---|
    | 同进程 `ctx.inject(["pluginManager"])` | **直接是结果对象**：`application` / `changed` / `version` / `bundle` / 可选 `error` —— **没有 `ok` 字段** |
    | 跨进程 remote 代理（官方「插件」页走这条） | 外面再包一层 `{ ok: true, value }` 或 `{ ok: false, error: { message } }` |

    写 `if (!answer?.ok) return { ok: false, ... }` 的话，同进程调用时 `answer.ok`
    是 `undefined`，`!undefined === true`，于是 package.json 已升、pnpm 已 exit 0、
    node_modules 里已是新版本，**返回值却报 "install failed"**。这个坑在 0.2.1 和
    0.2.2 里都有，实测踩了两次。

    正确写法：

    ```js
    const wrapped = typeof answer?.ok === "boolean"
    if (wrapped && !answer.ok) return { ok: false, error: answer?.error?.message }
    const result = wrapped ? answer.value : answer
    if (result == null) return { ok: false, error: "the plugin manager returned no result" }
    // 反方向的坑：manager 把失败**折进结果里**，不抛异常。
    // 只看「有没有异常」会误判成功。
    if (result.application === "failed" || result.application === "cancelled") {
      return { ok: false, error: result.error?.diagnostic ?? result.error?.code }
    }
    ```

    `application` 的取值：`applied`（HMR 生效）/ `restart-required`（带
    `restartReason`，如 `no-hmr`）/ `failed` / `cancelled`。

    **还有一层**：install 成功后，**正在执行这次调用的那个实例仍然是旧代码**。
    HMR 只重载被它管理的行，不会重载自己。所以修完 bug 再 apply，那次返回的
    还是旧代码算出来的结果 —— 要重启 DSH 才能看到修复生效。

22. **DSH 没有「装完插件就跑钩子」这回事**。`dsh.bundle` 的 schema 只有一个
    `patch` 字段；`dsh-plugin.json` 的 manifest 只有 `facets` / `requires` /
    `permissions` / `contributes` / `subscriptions`。所以「装完自动建 venv」
    不可能靠 npm 的 `postinstall` 实现（而且整机所有插件都不走那条路）。

    结论：环境准备必须**由插件自己的 Host 代码在运行期做**，或者暴露成一个
    Agent 能调的工具。本包两条都做了（`ui_locate_setup` + 设置页）。

23. **`python -m venv` 在受限环境里会因为 ensurepip 而失败**。报错是
    `Error: [WinError 5] 拒绝访问`，看起来像路径权限问题，实际是 ensurepip
    往 `Scripts/` 写的时候被拦。加 `--without-pip` 就干净了，然后自己把 pip
    补上：

    ```
    python -m venv --without-pip <dir>
    <dir>/Scripts/python.exe -m zipfile -e <pip-26.2.2-py3-none-any.whl> <dir>/Lib/site-packages
    ```

    pip 的 wheel 就是个 zip，用它自带的 `zipfile` 模块解开即可，不需要
    `get-pip.py`（那个要往临时目录写 `pip.zip`，同样是权限问题，连把
    `TEMP` 指到可写目录都绕不过 —— 实测两次都失败）。

24. **`pip install` 失败时第一件事是看 `TEMP` 指到哪**。受限环境里的典型报错是
    `ERROR: Could not install packages due to an OSError: [Errno 13] Permission
    denied: '...\pip-unpack-xxxx\xxx.whl.metadata'` —— 路径里带 `pip-unpack`
    就说明是解包目录写不进去，不是网络问题。把 `TEMP` / `TMP` / `TMPDIR`
    指到运行目录再试（`runtime.mjs` 里就是这么做的）。

25. **`/api/<插件名>` 比内核的 `/api` 长，会绕过内核准入检查**。`webServer`
    按最长前缀匹配，所以挂在 `/api/ui-locate` 下的路由**不走**内核那套鉴权。
    插件必须自己过一遍栅栏（`plugin/trust.mjs`）：只认 loopback 的 Host、
    拒 `sec-fetch-site: cross-site`、Origin/Referer 必须同源；桌面壳转发的
    `dsh-app://app` 单独放行。

    别指望「反正是本地服务」。浏览器里任何一个网页都能往 `127.0.0.1` 发请求，
    而这条路由能删 venv、能下文件。

26. **httpx 默认会读系统代理，于是访问 `127.0.0.1` 的请求被送去了代理**。

    实测过：服务还没起来时，`_probe()` 拿到的不是「连不上」，而是代理回的
    **503 空体**——服务真的起来之后同一个调用才变 200。也就是说探活被代理
    夹在中间了。

    这一层的可怕之处在于症状指不到代理身上：

    - 探活永远看不到自己的服务（`_probe()` 一直返回 `None`），于是每次调用都
      重新拉一遍服务，而端口其实早被上一个占着；
    - 或者代理自身挂了/拒绝转发，探活就永久失败，而服务其实好好的；
    - 或者干脆挂住，直到超时。

    Python 侧一律 `trust_env=False`（`mcp_server.py` 里的 `_http()` 与
    `_probe()`、`experiments/compare_text_mode.py` 的探针）；命令行里用
    `curl` 则要 `--noproxy '*'`。**别拿「这台机器没设代理」当理由省掉这个参数**
    ——换个环境、或者用户开了 Clash/系统代理，这条就会以完全不同的面貌回来。

27. **设置页选的东西传不到 MCP 子进程，除非你落个文件**。

    `cordis.patch.yml` 里给 MCP 客户端写的 `env` 是 `!!js` 表达式，而它**只在
    启动时求值一次**。用户在设置页把模型目录从 A 改成 B，子进程不会重启，于是
    它一辈子都按 A 找，报 `ModelNotFound`，而界面上明明显示 B。这类「界面说
    一套、进程做一套」的问题最难查——两边各自看起来都是对的。

    解法不是想办法重启子进程，而是**别把配置放在只在启动时读一次的地方**：
    插件准备完环境后写 `<运行目录>/runtime.json`，Python 每次解析路径时现读
    （`config.runtime_settings()`）。环境变量仍然优先，所以临时覆盖照样好使；
    文件坏了或者内容不是对象，一律当成没设过。

    写失败不影响功能，只是退回默认推导——也就是没设过时的行为。

28. **`__ModuleLoader__.load({id})` 的 id 必须等于包名，不是插件短名**。

    `client.js` 里我把 `id` 写成 `'ui-locate'`（顺手、也符合直觉），而包名是
    `ui-locate-mcp`。宿主侧一切正常：Host 半身跑起来了、工具都在、`/api/ui-locate/*`
    也回 200，只有设置里**那一栏从头到尾没出现过**。

    机制在 `@deepseek-ai/dsh-client-modules` 的 `arrive()`：它拿着**包名**去查

    ```js
    if (this.factories.has(id)) return "registered"
    failures.push(`${url}: loaded without registering "${id}" via __ModuleLoader__.load`)
    ```

    名字对不上 → 每个 URL 都试一遍 → 抛 `could not load "ui-locate-mcp"`。而这条
    错误**只进浏览器控制台**，宿主日志里一个字都没有。所以「设置页不出现」这个
    症状，在宿主侧查多久都查不出来。

    教训：**界面那一半出了问题，先看浏览器控制台，不要只看宿主日志。**
    `tests/check_bundle.py` 的 `[4c]` 现在会直接比对这个 id 与包名。

29. **写出去的东西要读回来，否则界面和进程各说一套**。

    坑 27 讲了为什么要把设置页的选择写进 `runtime.json`。但只写不读，等于
    把同一个问题换个方向又犯一次：`plan()` 从插件配置与默认值算路径，而
    `runtime.json` 里躺着用户上次选的真实位置——两边不一致时，界面显示
    「模型缺失」、「准备环境」按钮亮着，而 `D:\myLocalModels\ui-locate`
    里那份 77MB 的现成模型被完全忽略，点一下就是重新下一份。

    `readRuntimeSettings()` 把文件读回 `plan()` 的优先级链：

    ```
    本次显式传入 > 插件配置 > runtime.json > 默认值
    ```

    这里还有一个 `??` 与 `||` 的陷阱：插件配置的字段默认值是**空串**，
    而 `"" ?? x` 得到的是 `""`——`??` 只在 `null`/`undefined` 时让位。
    所以这条链必须用 `||`。

30. **回包前不读请求体，会把 keep-alive 连接毒化**。

    `Handler` 里 `protocol_version = "HTTP/1.1"`，响应又带 `Content-Length`，
    于是连接会被复用。只要有一条请求的 body 没读完，那些字节就留在 socket
    里，**下一条请求的请求行从它们中间开始解析**。

    实测形态非常迷惑：同一条连接上先 `POST /nope`（body `{}`）拿到 404，
    再 `POST /act` 收到

    ```
    501 Unsupported method ('{}POST')
    ```

    方法名里混着上一条请求的 body。httpx 默认复用连接，所以任何客户端都会
    踩到，不是边角情况。修法是把「读干净」放在回包出口（`_send` 里调
    `_drain()`），而不是散在十几个分支里——「记得每个分支都要读」这种事
    迟早会漏，而漏一次就让连接坏掉。

    顺带补上 `Transfer-Encoding: chunked`：只看 `Content-Length` 的话，
    分块编码的请求体会被当成「没有 body」，这是同一种污染，只是入口不同。

31. **挂在 `self` 上的请求体缓存必须按请求重置**。

    坑 30 修完之后立刻冒出这个：`BaseHTTPRequestHandler` 在 keep-alive
    连接上**复用同一个实例**跑 `handle_one_request()` 的循环，所以
    `self._body_cache` 会跨请求活着。不清就会读到上一条请求的体。

    表现比坑 30 更难推：同一条连接上 `POST /nope {"a":1}` 之后再
    `POST /act {"action":"wait"}`，后者报

    ```
    需要 text（按文字找）或 x 与 y（直接给坐标）
    ```

    ——连接没坏、状态码也正常（400 而非 501），只有内容对不上，看着像业务
    校验出错。修法是覆写 `handle_one_request()`，在 `super()` 之前把缓存
    清掉：这是**唯一**保证每条请求都经过、且在所有 `do_*` 之前的入口。

    这两个坑都有回归检查：`tests/check_http.py`（5 项），需要真起服务、
    真复用一条 TCP 连接，所以不能并进纯逻辑的 `test_act.py`。

32. **动手前别只认 `mosaic`，它漏框就等于那段文字没进 OCR**。`mosaic` 只对
    **检测框内**的裁片做 OCR —— 检测器一旦没框住目标，匹配就只能往下降档，
    于是查「插件」掉到 contains 档、命中任务栏上「视觉定位MCP插件开发」，
    点下去窗口获得焦点、屏幕确实变了、`verified: true`。**工具报告成功，
    实际点错了地方**，比报失败危险得多。

    两条独立防线，缺一不可：
    - `find(min_tier="starts")`：动手路径不接受 contains 档，宁可报「没找到」；
    - `_probe_hit` 在 mosaic 空手时自动退回 `full` 再找一遍 —— 多花两三倍
      OCR 时间，换「不点错」。只查不点的 `/locate` 不做这个回退，快更重要。

    排错时有个反直觉的地方：**别靠「读回光标」判断注入成没成**。目标程序
    捕获鼠标时那个值本来就不变，判据本身失效。要判成没成，比截图。

33. **后台任务没人接 rejection，会把宿主带走**。实测过一次：装上插件、重启
    DSH，MCP 那半身照常工作、`mcp__ui-locate__*` 全在，但 Host 那半身的工具
    （`ui_locate_setup`）**没出现**。宿主日志里什么都没有 —— 因为它是被打断的，
    不是报错退出的。

    现场是这样的链路：

    ```
    execFile spawn EPERM
      → installedPythonVersion 没挂 onRejected，rejection 传下去
      → syncPythonToBundle 的 await 抛
      → syncPython 的 .then 没有第二个参数
      → void syncPython()      ← void 只是「不等它」，不是错误处理
      → unhandledRejection     → Node 终止进程
    ```

    三条纪律，缺一条都会重现：
    - 凡是「可能起不来」的子进程，一律包在统一的 runner 里，把同步抛和回调
      失败都折成同一种结果。**注意两个 runner 不一样**：`core.mjs` 的
      `runProcess` 那个 Promise 带 reject，try 只是保险（拆掉测试仍绿，
      因为构造器会接住）；`runtime.mjs` 的 `run` 只 resolve 不 reject，
      拆掉 try 就真的漏（拆掉会红 2 条）。别照着一个改另一个。
    - 启动时发起的后台动作，`void fn()` 换成 `fn().catch(...)`。`void` 是
      语法糖，不接错误。
    - 自愈性的附加动作（同步 venv 版本这类）失败要折成 `state`，不要让异常
      冒到 `apply()`。宿主是拿「apply 跑完没有」判断插件活没活的。

    排查提示：**Host 半身的工具消失、MCP 半身照常**，这个组合基本就指向
    `apply()` 里炸了一次。验证方法是用假 ctx 跑一遍 `apply`（不必真启动 DSH）。

34. **`FrameStore.get(turn, 0) is None` 判不出「这个回合有没有帧」**。回合存在
    但零帧、和回合已被淘汰，两种情况下 `get` 都返回 `None`，含义完全不同：
    前者该补抓一张，后者什么都不该做（补也补不回被淘汰的回合）。要用
    `turns_of(turn)` —— 它返回列表，空列表就是「存在但没帧」，被淘汰则是
    空列表之外再加 `get() is None` 一起看。

35. **抓屏失败和动作失败是两条独立的真相，不能只报一条**。`act()` 里动作抛
    异常时走的是早退路径，曾经这条路径把 `capture_error` 丢掉了：调用方只
    看到 `stage: "act"`，于是去查目标窗口对不对 —— 而真正的问题可能是这个
    进程既抓不了屏也注入不了输入（`SendInput` 全部失败、`last_error=5`）。
    这两件事该查的方向完全相反。现在早退分支也会带上 `verify_unavailable`。

36. **更新只能有一条链路；两条必然互相拆台**。DSH 插件页的更新按钮只认
    registry 包（`updateRefusal()` 要求 `kind === "registry"`），所以依赖必须是
    `ui-locate-mcp` / `^X.Y.Z`，不能是 `github:owner/repo`。

    更隐蔽的是并行两条链路会自己打自己：0.5.x 自研更新器传的是
    `git+https://...#vX.Y.Z`，而 `installBundle` 收到 git spec 就会把 profile 的
    依赖写回 `github:` —— 于是它升一次，插件页的按钮就消失一次。表现为「升一次
    好一次、再升一次坏掉」，比完全没修更难查。

    0.6.0 已经把自研那套整段删掉（`updateCheck` 配置项、tracker、版本比较、
    registry 读取、`ui_locate_update` 工具）。**唯一的例外是 venv 里 Python 包的
    版本同步** —— 那一层宿主不管，必须留着（见 `docs/update-audit.md`）。

37. **`npm publish` 的三道门槛不会在打包时检查**。`private: true`（直接拒绝）、
    缺 `license` 字段、缺 `LICENSE` 文件。这三条对「用 git 装」完全无感 ——
    装得上、跑得动，只有真要发 npm 时才一次性全崩。所以 `check_bundle.py`
    现在把这三条并进 [1b] 段，让它们在**打包前**就红。

## 文件

| 文件 | 作用 |
|------|------|
| `package.json` | **bundle 声明**：`dsh.bundle.patch` 指向 `cordis.patch.yml`，`dsh.client` 声明客户端半身 |
| `cordis.patch.yml` | bundle 的 patch：`insert` 一个 `@deepseek-ai/dsh-mcp-client` 连接项 + 一个本包 Host 插件行 |
| `plugin/index.mjs` | **Host 插件**：版本检查、安装新版本、准备运行环境、挂设置页接口 |
| `plugin/tool.mjs` | 本仓库自带的 `defineTool`：宿主那几条会让插件起不来的硬规则都在这里 |
| `plugin/core.mjs` | 纯逻辑（版本比较、发布源、venv 同步、状态机），不依赖 Cordis，可直接用 node 测 |
| `plugin/runtime.mjs` | **环境准备**：建 venv、装依赖、下模型；外部世界全注入，所以能测 |
| `plugin/api.mjs` | 设置页的四条 HTTP 路由（状态 / 默认值 / 开始准备 / 轮询进度） |
| `plugin/trust.mjs` | 自建的信任栅栏：插件前缀绕过内核准入，得自己判一遍 |
| `client.js` | **客户端半身**：往 `settings.section` 注册「运行环境」一页 |
| `market-index.json` | 插件市场索引（format 1），在线导入的入口 |
| `locale/{en,zh}.json` | 插件列表里显示的标题与描述 |
| `icon.svg` | 插件图标（≤256 KiB，必须留在包内） |
| `src/ui_locate/mcp_server.py` | **MCP 入口**（FastMCP + stdio），薄客户端 + 自动拉起服务 |
| `src/ui_locate/config.py` | **运行期配置层**：模型路径、端口、provider、各类阈值的唯一来源；`runtime.json` 也在这里读（见坑 27） |
| `src/ui_locate/server.py` | 常驻 HTTP 服务，跑模型 |
| `src/ui_locate/screen.py` | **屏幕几何层**：DPI 感知、枚举显示器、按屏抓屏（唯一坐标权威） |
| `src/ui_locate/detect.py` | 单块检测核心，letterbox + NMS + 反变换 |
| `src/ui_locate/dml_fix.py` | **DirectML 兼容层**：改写导出模型里的 `allowzero`（详见坑 20） |
| `src/ui_locate/tiled.py` | 分块检测（含智能分割：切缝对齐 + 跳空白块） |
| `src/ui_locate/ocr_match.py` | 整页 OCR 与检测框做空间匹配、按文字查找、图标噪声剥离 |
| `src/ui_locate/mosaic.py` | 裁片拼图 OCR（快 4 倍的路线） |
| `src/ui_locate/annotate.py` | 把检出的元素裁片拼成带编号的总览图 |
| `src/ui_locate/act.py` | **动作编排**：定位 → 动作 → 验证，含等待 / 重试 / 逐步记账。零外部依赖，外部世界全注入，所以能纯 Python 测 |
| `src/ui_locate/actions.py` | **Win32 输入注入**：`SendInput` 与带坐标的 `PostMessage` 两条通道，键盘 / 鼠标 / 滚轮 / 拖拽 / 打字 |
| `src/ui_locate/frames.py` | **帧记忆**：按回合存原始画面（不存结论），支持按矩形裁片放大；见「帧记忆」一节 |
| `src/ui_locate/locate.py` | 抓屏包装 + OCR |
| `src/ui_locate/client.py` | HTTP 客户端 + CLI |
| `src/ui_locate/cuda_dlls.py` | 注册 nvidia 包里的 CUDA dll 目录 |
| `scripts/bootstrap.ps1` | **一次性环境准备**：建 venv、装依赖、下模型 |
| `scripts/make_index.py` | 生成 / 校验市场索引 JSON |
| `scripts/serve.ps1` | 手动起停常驻服务 / 查状态 |
| `tests/check_bundle.py` | bundle 结构静态自检（对照 DSH 的校验规则） |
| `tests/smoke_mcp.py` | stdio 全链路冒烟测试（8 项） |
| `tests/test_act.py` | 动作编排测试（63 例，`python -m unittest`）—— 全用桩，不碰真屏幕 |
| `tests/test_match.py` | 匹配档位、排序与动手前的两道防线（18 例）—— 守那次「掉到 contains 档点错窗口」的事故 |
| `tests/test_frames.py` | 帧记忆测试（16 例）：回合编号、整回合淘汰、负原点裁片 |
| `tests/test_integration.py` | **进程内集成测试**（10 例）：真 Handler + 真 HTTP，只把抓屏与输入换成桩 —— 真机 e2e 在受限进程里跑不动，靠这层守接线 |
| `tests/check_http.py` | HTTP 层回归检查（5 项）—— keep-alive 复用与 chunked 请求体（详见坑 30、31） |
| `tests/update-plugin.test.mjs` | 插件行为测试（69 例，`node --test tests/`） |
| `tests/stubs.mjs` | 测试脚手架：现场生成 `@deepseek-ai/*` 两个桩，不占版本控制 |
| `experiments/` | 一次性探测与校准脚本，每个都记了结论 |

## 多显示器与智能分割

**坐标系只有一个权威定义**：`screen.py` 模块 docstring 里那句
`屏幕绝对坐标 = origin + 图内坐标`。`origin` 是这块屏左上角在虚拟桌面里的位置，
主屏是 `(0, 0)`，副屏可能是负数（主屏在右、副屏在左时的典型布局）。

**按屏分别检测**：`/detect` 不带 `path` 时会遍历所有屏，**每块屏从自己的 (0,0)
独立规划分块网格**，各自加自己的 origin 再汇总。这样既不跨屏错位，也不去扫
显示器之间的空白区。

**智能分割**（`smart=1`，默认开）做两件事：

1. **切缝对齐**：先用 `Canny(60,160)` 算边缘图，统计逐列/逐行的"墨水密度"，
   然后把每条切缝在 ±`overlap` 范围内挪到密度最低的位置，避免把一个按钮劈成两半。
   实测：一块高分屏、元素横跨 `[640,800]` 时，纯几何切缝在 `720`（正好劈开），
   smart 把切缝挪到 `612`，穿过元素 0 条。
2. **跳过空白块**：整块都是纯色（边缘密度 < `MIN_EDGE_RATIO = 0.0008`）就不送推理。
   纯色块实测密度约 `0.0000`，一行小字约 `0.004`，满屏文本约 `0.05`。

代价：真实桌面全屏时多花约 300 ms（多了 Canny + 密度统计），但换来更少的
无效推理和更少的"元素被劈开"漏检。想回纯几何就传 `smart=0`。

## 一个不显然的收益：mosaic 与「文字密度」解耦

`full` 模式的耗时不只看分辨率，更看画面上有多少字。同样是 1567×881：

| 画面 | full | mosaic |
|------|------|--------|
| 游戏界面（文字少） | 442 ms | 505 ms |
| 桌面截图裁到同尺寸（文字密集） | 1927 ms | 505 ms |

`full` 要扫每一个像素找文字行，字越多越慢；`mosaic` 只扫检测框里的裁片，
耗时只跟「元素数量 × 面积」有关，与画面文字密度无关。
所以游戏 UI 里 mosaic 不一定更快（检测框大、裁片多），
而信息密集的应用界面里 mosaic 快 4 倍。

## 关于「在服务里顺手把点击也做了」

这件事做过两次，结论不一样，都留个记录。

第一次失败：在本机 Python 进程里调 `SendInput`，`SetCursorPos` 返回失败、错误码
0、光标一步不动（实测 0/6）。当时的结论是「定位和输入是两件事，各有各的强手」。

后来发现结论下早了。同一条代码，在**桌面程序处于前台**时 `cursor_moved: true`，
换到全屏游戏捕获鼠标时又全部失灵。也就是说**限制来自前台那个程序对鼠标的独占，
不是本进程的权限**，换一个前台就可能变。所以现在服务里保留了 `actions.py`，
提供 `send_input` / `message` 两条通道，由调用方按场景选。

真正该记住的不是「能不能做」，而是**别靠 `ok` 判断做没做成**：
- `ok` 只表示编排跑完了；
- `verified` 才是「屏幕上真的变了没有」；
- 抓屏被拦时 `verified` 给 `null`（没法判断），**不要当失败重试** —— 可能点两次。

仍然推荐：拿不准的时候，定位交给这套工具，点击交给 `cua_driver_native__click`。
它绕开了前台锁的问题，代价是多一次往返。

## find() 的排序：长度比置信度更可信，且要先剥图标噪声

查「插件」时命中过两次错的东西，是两个独立的问题。

**问题一：长文本压过短控件。** 排序只看 `conf`：

```
正文段落「这次"插件"没有点中…」  conf=0.99   ← 排第一
按钮「$ 插件」                  conf=0.46   ← 被挤掉
```

控件名的特点是**短**。改成先按「文字长度与查询词的长度差」升序、再按 conf 降序，
分层改为 完全相等 > 以查询词开头/结尾 > 包含。

**问题二：图标被 OCR 认成字符，精确匹配永远不成立。** 侧栏按钮的文字是「插件」，
但前面有个图标，OCR 有时读成 `8`、有时 `$`、有时 `④`，于是候选变成
`8 插件` / `$ 插件` / `④插件`。「完全相等」这一档永远不成立，全部降级到
「包含」档，然后输给了右上角真的包含两个字的 `+添加插件`。

修法是匹配前先剥掉首尾的噪声字符（`ocr_match._strip_icon_noise()`）：
空白、短文本里的孤立数字、`0x2000-0x2BFF` 符号区、`0xE000-0xF8FF` 私用区、
ASCII 标点、全角标点。**只剥首尾不动中间**——中间的多半是真实内容；
剥空就退回原串，避免把真的只有符号的识别结果抹掉。

修完之后的实测：

```
locate_text("插件")  →  matched='插件'  (-1086, -909)   ← 侧栏按钮，正确
locate_text("新会话") →  matched='新会话' (-1088, -961)
locate_text("设置")   →  matched='设置'   (1452, 1775)
```

同一套修法在 `mosaic` 和 `full` 两种 OCR 模式下都生效。
