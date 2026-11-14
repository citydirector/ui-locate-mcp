# ui-locate-mcp

[English](README.md) · **简体中文**

[![npm](https://img.shields.io/npm/v/ui-locate-mcp)](https://www.npmjs.com/package/ui-locate-mcp) [![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![DSH MCP plugin](https://img.shields.io/badge/DSH%20MCP%20plugin-Windows-blue)](https://github.com/topics/dsh-plugin)

在本机屏幕上按文字查找界面元素，返回可用于点击的像素坐标。它也提供定位后点击、输入、滚动和验证的组合工具，适合需要操作桌面界面的 DSH agent。

检测、OCR 和动作服务在本机运行。首次准备环境需要下载 Python 依赖和检测模型；模型约 80 MB。常驻服务负责推理，MCP 进程负责转发请求。

## 快速开始

1. 在 DSH「插件 → 添加插件」输入 `ui-locate-mcp`，安装并启用。
2. 在设置中打开 **ui-locate 运行环境**，选择目录和后端，点击「准备环境」。也可以调用 `ui_locate_setup`。
3. 等待环境准备完成，再连接或重新启用 `mcp-ui-locate`；若 DSH 提示需要重启，按提示操作。
4. 先调用 `health`，再尝试 `locate_text(text="保存")`。

这个 bundle 包含 MCP 客户端、Host 运行环境管理插件和 Client 设置页。npm 安装只取得代码，不会自动备齐 Python 环境和模型。

### 环境要求

- 当前 bundle 的 MCP 启动路径按 **Windows** 配置；不要把 Python 核心的跨平台能力当作 bundle 的跨平台安装保证。
- Python 项目要求 **Python 3.12+**。设置页使用宿主能找到的 Python 创建独立环境；找不到可用解释器时，先检查桌面安装附带的运行时。
- 默认后端是 **DirectML**；另有 `cuda` 和 `cpu`。CUDA 需要相应的 NVIDIA 环境，依赖体积更大。切换后端需要重建环境。
- package.json 目前没有声明 `engines.dsh`。不能据此断言所有 DSH 版本都兼容；本包需要宿主提供工具注册、MCP 客户端及设置页服务。

命令行安装到 Web profile：

~~~sh
dsh plugin --profile web add ui-locate-mcp
~~~

命令行操作前关闭对应的 DSH 实例，完成后重启。Desktop 用户优先使用插件页；管理 desktop profile 时使用该桌面安装自带的 CLI 和相同的 DSH_HOME。

## 工具

| 工具 | 用途 |
| --- | --- |
| `locate_text` | 按文字查找匹配元素及其中心坐标 |
| `find_all` | 返回同名或相似文字的多个候选 |
| `detect_elements` | 枚举画面中检测到的元素 |
| `ui_locate_image` | 分析调用方已有的图片：找文字或枚举元素，坐标按图片像素算 |
| `ui_locate_marks` | 带编号框（可叠加带名字的网格）的截图，让模型选编号而不是估像素 |
| `ui_locate_act` | 定位后执行点击、输入、滚动等动作，并尝试验证 |
| `ui_locate_steps` | 串行执行一组步骤，返回失败位置 |
| `ui_locate_windows` | 列出顶层窗口（标题、句柄、客户区、是否前台） |
| `ui_locate_diff` | 上次调用之后屏幕哪里变了，回矩形（不回图、不花视觉 token） |
| `ui_locate_watch` | 录几秒，回一张关键帧拼图，只贴变化区域 |
| `ui_locate_frames` / `ui_locate_frame` | 列出或取回近期动作帧 |
| `list_monitors` | 显示器尺寸、原点和索引 |
| `health` | 检查服务是否就绪 |
| `ui_locate_setup` | 准备或重建 Python 环境与模型 |

除 `ui_locate_setup` 由 Host 插件提供外，表内其余都是 MCP 工具；实际调用名称可能带宿主添加的命名空间。

~~~python
locate_text(text="保存", monitor="primary")
ui_locate_act(text="保存", action="click", monitor="primary")
~~~

没有稳定文字的目标（游戏物品、工具栏图标、地图单位）先标注，再按编号操作：

~~~python
ui_locate_marks(monitor="primary")          # 带编号框的截图
ui_locate_act(mark=17)                       # 点 #17，坐标由检测器算
ui_locate_marks(box="1200,40,1500,90")       # 放大一条小按钮（最多 4 倍）
ui_locate_marks(grid=8)                      # 没框出来？选一格，比如 D3
ui_locate_marks(box="D3", grid=4)            # 放大那一格，再选编号或更小的格
ui_locate_act(cell="B2")                     # 点某一格的中心
~~~

除了检测器的框，编号还包括纯几何规则找出的「成排成列、同尺寸」格子（物品栏、标签页、技能栏、表格，`kind: "cell"`），这类东西 UI 检测器在游戏里通常认不出。编号只属于生成它的那张图；带上 `mark_set` 可拒绝过期的编号。

坐标使用屏幕绝对位置，多显示器可能返回负坐标。`monitor` 可为 `primary`、索引字符串、`all`、`window:<标题>` 或 `hwnd:<句柄>`。界面移动后应重新定位，不要长期复用旧坐标。

### 分析手上已有的图片

当调用方自己就拿着像素 —— 别处截的图、用户贴进来的图片、别的工具产出的帧 —— `ui_locate_image` 直接回答，不碰这台机器的桌面。同一套契约也走 HTTP：`POST /locate-image`。

请求：`image_b64`、`width`、`height`、`text`、`mode`（`"locate"` 或 `"detect"`），可选 `limit`、`conf`、`tiled`。其它字段一律拒绝（`unsupported_field`）而不是忽略：写 `path` 或 `window` 的人会以为它生效了，默默容忍只会教出错误的用法。

响应形状固定为契约版本 1：

~~~json
{"ok": true, "contractVersion": 1, "coordinateSpace": "image-pixels",
 "image": {"width": 1280, "height": 720},
 "matches": [{"text": "保存", "confidence": 0.91,
              "box": {"x": 20, "y": 30, "width": 84, "height": 26},
              "center": {"x": 62, "y": 43}}]}
~~~

- `width` / `height` 必须等于解码出来的图片尺寸。不一致报 `dimension_mismatch`，不会悄悄缩放：调用方后面算的每个坐标都依赖这个数。
- 坐标是整张图片左上角起算的像素 —— 不叠加显示器原点、不做归一化 —— 每个框都夹在图片范围内。
- `mode: "locate"` 要求 `text` 非空；`mode: "detect"` 可以不给，也可以给空串。
- locate 用的是**和动作同级**的匹配档，不是「只查不动」那套宽松档：完全相同，或查询词出现在识别文本的开头/结尾（OCR 常把图标拼在按钮前，「8 插件」查「插件」会命中）。查询词只出现在中间的文本一律不算 —— 拿到匹配的人通常会直接去点，而「碰巧包含这两个字」正是点错窗口的由来。检测框裁片的 OCR 没读出字时，会整图再读一遍才回答「没找到」；命中只跑一遍。
- `matches` 最优在前（完全相同优先于前缀/后缀，再比文本长度接近程度，最后才比置信度），所以 `matches[0]` 就是要操作的目标；也正因如此，confidence 不保证单调递减。
- 没有匹配是**成功**，返回 `matches: []`，不会执行任何动作。
- 限额：请求体 32 MiB、图片 24 MiB、4000 万像素、单边 16384 px、`limit` 最多 500。
- 错误码：`bad_json`、`bad_request`、`unsupported_field`、`bad_base64`、`bad_image`、`unsupported_format`（只收 PNG / JPEG）、`too_large`（413）、`dimension_mismatch`、`missing_text`、`bad_mode`、`busy`（503，引擎超时）、`engine_error`（500）。失败不缓存，也绝不返回半份匹配结果。

这条路由不接受 `path`、`monitor`、`window`、`capture`、`focus`，不读文件、不抓屏；桌面侧被关掉时它照样工作。

### 比较两张图片

某一步到底有没有改变画面，问的是**像素**，不是文件字节：同一张图换一个压缩器重编码，或者多一个 `tEXt` 块，字节完全不同而像素一模一样。`POST /compare-images` 解码两张图后逐像素比较 —— 不跑检测、不跑 OCR、不抓屏、不加载模型，所以模型没起来时它也照样回答。

请求：`{"before": {"image_b64", "width", "height"}, "after": {...}}`，严格白名单，限额与 `/locate-image` 完全一致。每侧声明的尺寸必须等于图片头里的尺寸（否则 `dimension_mismatch`），而且**先看文件头再解码** —— 声明 `1×1` 塞一张巨图绕不过像素上限。

~~~json
{"ok": true, "contractVersion": 1, "comparable": true, "changed": false,
 "changedPixels": 0, "totalPixels": 921600, "changedRatio": 0.0,
 "maxChannelDelta": 0, "changedBox": null,
 "image": {"width": 1280, "height": 720},
 "before": {"width": 1280, "height": 720},
 "after":  {"width": 1280, "height": 720}}
~~~

- `changedPixels` 统计**任一通道**不同的像素，**没有容差**：差一级也算变了。多大的变化才算"有意义"交给调用方，配合 `changedRatio`、`maxChannelDelta`、`changedBox` 自己判断。
- 尺寸不同就是**不可比**：HTTP 200 + `comparable: false` + `changed: null`（附 `reason: "dimension_mismatch"`）。不是 `false` —— `false` 的含义是"我比过了，没动"。缩放过的两张图两者都不是，绝不能记成"验证成功"。
- `comparable: false` 仍然是一个格式正确的请求（两张图各自合法，只是尺寸不同）。400 留给写错的输入：`unsupported_field`、`bad_base64`、`unsupported_format`、`dimension_mismatch`（声明≠解码）、`too_large`。
- `/health` 会报出 `capabilities.compare`，客户端可以在**动手之前**就拒绝，而不是点到一半才发现服务没法验证结果。托管模式下这条路由照常可用：它不碰桌面、不注入输入。

### 只对一个窗口操作

后台投递必须绑定窗口：传 `hwnd=`（来自 `ui_locate_windows`）或 `window=`。只给屏幕坐标会被明确拒绝（`reason: "hwnd_required"`）—— 服务不会去猜你指的是哪个窗口。

`background` 内部的通道顺序是固定的：元素有 `InvokePattern` 就走 UI Automation（`channel: "uia-invoke"`）；只有**确定的**空手（`no_pattern`、`no_element`、UIA 根本不可用）才落到目标窗口消息（`channel: "message"`）。UIA 超时或报错会被报成 `code: "BACKGROUND_UNKNOWN"` + `dispatched: null` —— 动作可能已经发生，所以一条都不重发。消息通道只对白名单开放（`#32770`、WinForms 根窗口，以及 `Button`、`Edit`、`ComboBox`、`SysListView32`、`msctls_*` 这类经典控件）；Chromium、WinUI3、UWP、游戏与自绘界面一律 `BACKGROUND_UNAVAILABLE`，绝不回落前台。鼠标序列之前可能补发一条 `WM_ACTIVATE`，让控件认为自己处于激活状态 —— `activate="auto"`（默认，目标本来不是前台才发）/ `never` / `always`；它不调 `SetForegroundWindow`，但极少数应用收到后会自己去抢前台，遇到就把 `activate` 设为 `"never"`。有模态弹窗时，消息发给弹窗（`GetLastActivePopup`）、坐标也按弹窗换算，不会再点到被禁用的父窗口上。

`ui_locate_pause(reason)` / `ui_locate_resume()`（或 `POST /pause` / `/resume`）会停掉所有输入路径 —— 前台与后台都停 —— 直到恢复前一律回 `code: "PAUSED"`；`/health` 会报 `paused`。进程级热键这一轮还没做。

给 `ui_locate_marks`、`ui_locate_act`、`ui_locate_diff` 传 `window=`（标题或其中一段，或句柄）：

- 抓图先走 Windows Graphics Capture：直接从合成器拿窗口画面，游戏被聊天窗口盖住也能抓到，GPU 渲染的内容也在，裁成客户区。每个窗口保持一个常驻会话（空闲 15 秒关闭），watch / wait 连续抓几乎零开销。WGC 失败或全黑时退到 `PrintWindow`，再退到按屏抓再裁。`UI_LOCATE_WINDOW_CAPTURE` / `"window_capture"`：`auto`（默认）、`wgc`、`printwindow`、`screen`。WGC 需要 `windows-capture` 包，插件用 `--no-deps` 安装（它的元数据要求 `opencv-python`，和 `opencv-python-headless` 冲突）。Win10 上窗口周围可能出现黄色捕获边框，Win11 会关掉。
- 输入通道与焦点是两件事（`delivery=`）。`auto`（默认）走前台路径，点击仅在**明确观察到光标没动**时才补一次窗口消息。`foreground` 恒定走前台路径，可能移动真实指针。`background` 只投窗口消息 —— 不切前台、不动真实指针 —— 窗口吃不下时明确返回 `BACKGROUND_UNAVAILABLE`（UIA 结果不可知时是 `BACKGROUND_UNKNOWN`），绝不悄悄升级成 SendInput。后台目前只覆盖点击类（单击/双击/右键/中键）；键盘、滚动、拖动、移动需要 `foreground`。它是消息投递，不是保证：有些框架会忽略投递的点击，所以看 `dispatched`。不给 `window=` 时，鼠标动作会切到点击点下面的窗口；`focus="off"` 关闭。结果里 `performed.focus` 报告是否成功。
- 用 `window=` 标注的编号会记住这个窗口，之后 `ui_locate_act(mark=…)` 自动切前台、按窗口验证。

### 等待与验证

`verify="screen"` 且不给 `settle_ms` 时，动作后等画面停下来再比对（32px 网格差分，最多 1.5 秒；结果里有 `settle.stable` / `elapsed_ms`），不再固定等 220ms。验证结果会列出变化区域 `regions`；平均差被稀释、但目标旁边有一块明显变化时也算生效（`by: "region"`）。`action="wait"` 不给文字 = 等画面静止。动作前会先静置约 80ms 量一下画面自己在动多少（水面、粒子、视频）；这类画面（`verify.noise.animated`）上阈值随噪声抬高，本来就在动的格子不计，点空白动态背景不会再报 `verified: true`。

结果把两个问题分开：`dispatched` 说明输入有没有送达目标（投递的消息被接受、或 SendInput 真的发了事件），`verified` 说明画面随后有没有确认预期的变化。`verified: false` **不会**自动重放 —— 模拟输入有副作用，因为像素没变就再点一次，正是同一个按钮被点两下的由来。

### 先看几秒再动手

很多操作的结果没法从一张截图预判（地图会不会动、怪往哪走、技能是否在冷却）。`ui_locate_watch` 连录几秒（`duration_ms`、`fps`），用网格差分挑关键帧，只回**一张**图：第一帧全景，之后每个关键帧只贴变化区域的裁片，标着时间和屏幕坐标。画面不动只回一帧；几乎每帧都在变的格子（水面、粒子、时钟）当作环境噪声不计（结果里的 `ambient`）。`ui_locate_act(..., watch_ms=2000)` 在输入发出前开录，动作和 settle 都结束（且录满 `watch_ms`）才停，动作过程本身也录进去，「看 → 做 → 看」一次调用完成。`regions` 模式的拼图最后补一张末帧全景，各关键帧的变化区域用框标出，方向和距离一眼可见。

`ui_locate_marks` 默认增量：同一块屏或同一个窗口，先和上一次比差分，没变就整份复用；变化不到 30% 时只对变化区域（以及跨在边界上的元素）重跑检测和 OCR。返回里 `incremental.mode` 是 `reuse` / `partial` / `full`；编号每次都重新分配。partial 之前先预估开销（`incremental.estimated_cost`：`area_frac`、`text_frac`、`partial_ms` 对比 `full_ms`，并用上一次 partial 的实测耗时校准）。OCR 开销跟着文字走、不跟面积走，所以文字密集页上变化压在文字上时直接走 `full`（`reason: partial_not_cheaper`）。`incremental=false` 返回 `{mode: full, forced: true}`。

## 配置与数据目录

Host 行 ID 是 `ui-locate-update`，MCP 行 ID 是 `mcp-ui-locate`。

| Host 配置 | 默认 | 说明 |
| --- | --- | --- |
| `pythonSync` | `true` | 将环境中的 Python 包同步到 bundle 声明的版本 |
| `runtimeDir` | 空 | Python 环境、状态与日志目录 |
| `modelDir` | 空 | 模型目录，可复用已有模型 |
| `backend` | 空 | 沿用已保存选择；首次默认 DirectML |

默认运行目录按 `UI_LOCATE_HOME` → `DSH_HOME/ui-locate` → `LOCALAPPDATA/ui-locate` 选择。目录留空可跟随便携版的数据目录；显式绝对路径需要自行维护。

Python 服务还识别 `UI_LOCATE_MODEL`、`UI_LOCATE_MODEL_DIR`、`UI_LOCATE_PROVIDER` 和 `UI_LOCATE_PORT`，详见 [config.py](src/ui_locate/config.py)。MCP 默认连接本机 7788 端口。给模型的图统一是 JPEG 质量 90、色度不降采样（4:4:4），彩色小字不会糊；视觉 token 只看像素尺寸，不看文件大小。`UI_LOCATE_JPEG_QUALITY` 可改质量。

### 抓屏方式

`UI_LOCATE_CAPTURE`（或状态目录 `runtime.json` 里的 `"capture"`）决定怎么截图：

| 值 | 方式 | 什么时候用 |
| --- | --- | --- |
| `auto`（默认） | DXGI → WGC → GDI，失败或全黑就换下一个，并按屏记住上次成功的方式 | 绝大多数情况 |
| `dxgi` | Desktop Duplication | 最快；能抓独占全屏的 D3D 游戏 |
| `wgc` | Windows Graphics Capture（需要 `dxcam[winrt]`） | 混合显卡笔记本上 DXGI 报 `UNSUPPORTED` 时 |
| `gdi` | BitBlt | 兼容性最好；全屏游戏常抓成全黑 |

`health` 的 `screen.capture` 会显示每块屏上次用的方式。记住的降级后端每 30 秒会重新试一次 DXGI，一帧抽风不会让这块屏永久降级。HTTP 请求（`/marks`、`/act`、`/steps`、`/diff`、`/detect`）也可以带 `"capture": "gdi"` 之类，只对这一次生效。

目标窗口以管理员运行而服务不是时，Windows 会静默丢弃模拟输入（UIPI）。`health.integrity` 显示服务自己的级别，`/windows` 显示每个窗口的，对这种窗口的动作会返回 `uipi.blocked: true` 和 warning。

### 游戏里的输入

全屏游戏常用 Raw Input / DirectInput 读鼠标键盘，桌面程序认的那套它不认。下面两项可以每次调用单独指定（`pointer=`、`keys_mode=`），也可以全局设：

| 设置 | 取值 | 什么时候用 |
| --- | --- | --- |
| `UI_LOCATE_POINTER` / `"pointer"` | `absolute`（默认）、`relative` | 全屏游戏里点了没反应、光标不动。`relative` 像真鼠标一样把光标推过去，移动期间临时关掉加速并按回读闭环修正；结果里 `tracking: false` 表示游戏锁住了系统光标，位置是按 1:1 估算的 |
| `UI_LOCATE_KEYS` / `"keys"` | `vk`（默认，同时填扫描码）、`scancode` | DirectInput 游戏按键没反应 |

`action="hold"` 按住键 `duration_ms` 毫秒（走路、蓄力）。纯键盘动作不需要目标，屏幕验证改为整屏比对。

### 托管模式

托管开关（`src/ui_locate/managed.py` 的 `MANAGED_ENV`）告诉 Python 服务：这台机器的桌面归 Host 管。此时所有自主桌面能力 —— 抓屏、列显示器与窗口、marks、watch、diff、steps、frames、切前台、输入 —— 一律 HTTP `403` + `{"ok": false, "code": "CUA_MANAGED", ...}`，并回显被拒的路由、指向 Host 的做法。拦截发生在 HTTP 进程内部、路由之前，所以直接 POST `/act` 和走 MCP 工具被拒得一模一样；MCP 侧另有一次检查，让过期客户端不必多跑一个来回就知道原因；`/health` 也不再枚举屏幕、不再探测进程完整性。

只留三样：`GET /health`、`POST /locate-image`、`POST /compare-images`。`health` 报出 `contractVersion`、`managed` 和 `capabilities`（`{"image": true, "compare": true, "desktop": false}`），Host 可以先确认对端确实执行这条边界、而且能验证结果，再发任何东西。Host 应把 `capabilities.desktop === false` 也纳入握手，而不只看 `managed: true`：它为 `true`、或整个 `desktop` 键缺失，都说明应答的不是真正执行这条边界的那个进程（例如同一端口上残留的旧独立实例），此时正确做法是拒绝这个实例，而不是降级使用。health 里没有 `contractVersion` 同样是旧进程：重启它，不要拿来用。

开关是每个请求现读的，所以做决定的是**真正在服务这次请求的进程**，不是当初启动它的人。不设这个开关，独立模式保留全部能力。

同时保留托管与独立实例的 Host 会给托管实例单独一个端口，并在发任何请求前先问 `/health`：`7788` 上可能还挂着守卫管不到的旧独立进程。

## 更新

**插件本身由 DSH 插件页更新，本包不再自带更新器。** 依赖是 registry 形式
（`ui-locate-mcp`），插件页那一行就会出现原生更新按钮，走宿主自己的插件管理器。

本包只负责宿主不管的那一半：npm 包装到新版之后，venv 里那份 Python 包还是旧的，
所以每次启动会比对版本并 `pip install --no-deps` 重装一次（可用 `pythonSync` 关掉）。
放在启动时也自带容错 —— 这一次失败不影响 JS 侧已经装好，下次会再试。

0.5.x 及更早的版本里自己维护过一整套更新器（读版本列表 + 调 `installBundle`）。它被删掉
是因为和插件页的按钮是两条并行链路：各自判断什么是最新，而且它装完会把依赖写法
改回 `github:`，插件页的按钮随即消失。历史核对见[更新机制审计](docs/update-audit.md)。

## 排障与使用边界

- MCP 启动失败：先检查运行环境是否准备完成、虚拟环境 Python 是否存在，再重连 MCP。
- 动作结果中的 `ok` 表示执行流程完成；只有对应的验证结果才能说明界面是否变化。`verified: false` 或 `null` 时不要当作操作成功。
- `delivery="background"` 需要 `hwnd=`/`window=`，且只覆盖点击类。它是「UIA + 窗口消息」路线，不是「Windows 上通用后台输入」：有些框架会忽略投递的消息，UIA 超时会被报成 `BACKGROUND_UNKNOWN` + `dispatched: null` —— 不要盲目重试。
- `ui_locate_pause` 一次停住所有输入路径；暂停期间 `/health` 照常回答，只是 `paused: true`。
- 打开托管开关时，大多数工具返回 `CUA_MANAGED` 是设计如此，只剩 `/health`、`/locate-image`、`/compare-images`。Host 侧的 CUA 桥接已于 2026-11-14 撤回 —— background 投递会让 native 宿主退出（`NATIVE_EXITED` / `NATIVE_CLEANUP`），现在改由本包自有实现（`src/ui_locate/actions.py`）承担。
- 桌面权限、前台焦点、DPI 和应用框架会影响输入与截图。定位命中也不保证目标接受合成输入。
- 不同 ONNX Runtime 后端共用同名 Python 包；换后端应重建，避免叠装。
- OCR 有漏检和误识别；重要动作后检查画面或目标文字。

## 开发

~~~sh
python tests/check_bundle.py
node --test tests/
python -m unittest discover -s tests -p "test_*.py"
~~~

Python 测试需要相应依赖；真实模型和桌面冒烟还需要准备好的环境。历史性能与算法排障记录已移到[技术笔记](docs/technical-notes.md)，不作为当前机器的性能承诺。

当前 [release.yml](.github/workflows/release.yml) 按版本标签或手动指定标签发布 npm。发布时需保持 package.json 与 pyproject.toml 的版本一致。

## 相关项目

[@citydirector](https://github.com/citydirector) 的其它 DeepSeek Harness（DSH）项目：

| 项目 | 类型 | 用途 |
|---|---|---|
| [DSH-build](https://github.com/citydirector/DSH-build) | 构建 | DSH 的 Windows 便携版 / 桌面版绿色构建，每天跟随上游打补丁、冒烟、发布 |
| [dsh-remote-access-proxy](https://github.com/citydirector/dsh-remote-access-proxy) | 插件 | 在手机、平板或其它电脑上用浏览器访问 DSH Web GUI |
| [deepinfra-fusion-proxy](https://github.com/citydirector/deepinfra-fusion-proxy) | 插件 | DeepInfra Standard / Flex 按会话切换与自动回退 |
| [dsh-loop-rescue](https://github.com/citydirector/dsh-loop-rescue) | 插件 | 模型复读时从流内切断、剪掉循环并自动续跑 |
| [dsh-pwsh-plugin](https://github.com/citydirector/dsh-pwsh-plugin) | 插件 | 常驻 pwsh 会话工具（`pwsh_run`），附带给 `bash` 补写边界的 WSL 垫片 |
| [dsh-session-manager](https://github.com/citydirector/dsh-session-manager) | 离线工具 | 离线升级、归档、回收站、清洗循环、修复烂尾 / 隐藏会话 |

## 许可证

[MIT](LICENSE)
