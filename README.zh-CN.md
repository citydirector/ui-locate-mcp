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
| `ui_locate_marks` | 带编号框（可叠加带名字的网格）的截图，让模型选编号而不是估像素 |
| `ui_locate_act` | 定位后执行点击、输入、滚动等动作，并尝试验证 |
| `ui_locate_steps` | 串行执行一组步骤，返回失败位置 |
| `ui_locate_windows` | 列出顶层窗口（标题、句柄、客户区、是否前台） |
| `ui_locate_diff` | 上次调用之后屏幕哪里变了，回矩形（不回图、不花视觉 token） |
| `ui_locate_frames` / `ui_locate_frame` | 列出或取回近期动作帧 |
| `list_monitors` | 显示器尺寸、原点和索引 |
| `health` | 检查服务是否就绪 |
| `ui_locate_setup` | 准备或重建 Python 环境与模型 |

前十二项由 MCP 服务提供，最后两项由 Host 插件提供；实际调用名称可能带宿主添加的命名空间。

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

### 只对一个窗口操作

给 `ui_locate_marks`、`ui_locate_act`、`ui_locate_diff` 传 `window=`（标题或其中一段，或句柄）：

- 抓图走 `PrintWindow`，游戏被聊天窗口盖住也能抓到（只有客户区）。抓成全黑（独占全屏、部分 Vulkan 游戏）时自动改为按屏抓再裁。`UI_LOCATE_WINDOW_CAPTURE` / `"window_capture"`：`auto`（默认）、`printwindow`、`screen`。
- 发输入前先把目标窗口切到前台（SendInput 只送给前台窗口）。不给 `window=` 时，鼠标动作会切到点击点下面的窗口；`focus="off"` 关闭。结果里 `performed.focus` 报告是否成功。
- 用 `window=` 标注的编号会记住这个窗口，之后 `ui_locate_act(mark=…)` 自动切前台、按窗口验证。

### 等待与验证

`verify="screen"` 且不给 `settle_ms` 时，动作后等画面停下来再比对（32px 网格差分，最多 1.5 秒；结果里有 `settle.stable` / `elapsed_ms`），不再固定等 220ms。验证结果会列出变化区域 `regions`；平均差被稀释、但目标旁边有一块明显变化时也算生效（`by: "region"`）。`action="wait"` 不给文字 = 等画面静止。动作前会先静置约 80ms 量一下画面自己在动多少（水面、粒子、视频）；这类画面（`verify.noise.animated`）上阈值随噪声抬高，本来就在动的格子不计，点空白动态背景不会再报 `verified: true`。

## 配置与数据目录

Host 行 ID 是 `ui-locate-update`，MCP 行 ID 是 `mcp-ui-locate`。

| Host 配置 | 默认 | 说明 |
| --- | --- | --- |
| `pythonSync` | `true` | 将环境中的 Python 包同步到 bundle 声明的版本 |
| `runtimeDir` | 空 | Python 环境、状态与日志目录 |
| `modelDir` | 空 | 模型目录，可复用已有模型 |
| `backend` | 空 | 沿用已保存选择；首次默认 DirectML |

默认运行目录按 `UI_LOCATE_HOME` → `DSH_HOME/ui-locate` → `LOCALAPPDATA/ui-locate` 选择。目录留空可跟随便携版的数据目录；显式绝对路径需要自行维护。

Python 服务还识别 `UI_LOCATE_MODEL`、`UI_LOCATE_MODEL_DIR`、`UI_LOCATE_PROVIDER` 和 `UI_LOCATE_PORT`，详见 [config.py](src/ui_locate/config.py)。MCP 默认连接本机 7788 端口。

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
