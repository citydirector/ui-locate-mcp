# ui-locate MCP

本机视觉定位引擎的 MCP 封装。给一段界面文字，返回它的**精确像素坐标**，
用来替代「反复截图 → 估计位置 → 盲试坐标 → 再截图验证」的循环。

**打包形态**：DSH 的第三种插件形态 —— **MCP 连接型 bundle**（`package.json` 里
`dsh.bundle.patch` 指向 `cordis.patch.yml`，文件本身没有 Host / Client 入口）。
所以它既能像普通 MCP 一样手写进 profile，也能做成**插件市场索引**让用户在
「插件 → 发现」里一键安装。两条路都写在下面的「安装」章节。

```
locate_text("保存")  →  {"x": 1423, "y": 290, "w": 78, "h": 51}
cua_driver_native__click(scope="desktop", x=1423, y=290)  →  点中了
```

## 为什么需要它

视觉模型看缩略图时没有像素标尺，受缩放 / DPI / 负坐标影响，只能估个大概范围再试。
这个引擎把「看」拆成两步：**YOLO 找框（给坐标）** + **OCR 认字（给标签）**，
于是「点某个按钮」变成一次文本查询，坐标是像素级的。

## 工具

| 工具 | 用途 |
|------|------|
| `locate_text(text, conf, text_mode, monitor, smart)` | **最常用**。按文字查坐标，返回中心点 |
| `detect_elements(conf, text_mode, tiled, monitor, smart)` | 不知道叫什么时用：列出屏幕上所有元素 |
| `find_all(text, img_path, conf, text_mode, monitor, smart)` | 同名元素有多处时，看全部候选自己挑 |
| `list_monitors()` | 有几块屏、各自尺寸与原点（副屏原点可能是负的） |
| `health()` | 服务状态：是否就绪、哪个后端、预热多久 |

`monitor` 取值：`""` 或 `"all"`＝所有屏，`"primary"`＝主屏，`"0"` / `"1"`＝按索引。
**返回的 `x` / `y` 已是屏幕绝对坐标**，副屏原点是负数时也已经算进去了，
可以直接交给 `cua_driver_native__click(scope="desktop", ...)`。

配套调用：

```python
locate_text("插件")
# → {"ok": true, "matched": "插件", "x": 147, "y": 166, "w": 279, "h": 47}

cua_driver_native__click(scope="desktop", x=147, y=166)
```

副屏（原点为负）也一样，不需要换算：

```python
list_monitors()
# → [{"index":0, "w":2560, "h":1440, "x":0,    "y":0,   "primary":true},
#    {"index":1, "w":1920, "h":1080, "x":-1920,"y":-1080}]

locate_text("插件", monitor="1")
# → {"x": -1087, "y": -909}     副屏负坐标，直接可用
cua_driver_native__click(scope="desktop", x=-1087, y=-909)  →  点中了
```

## 架构：MCP 进程是薄客户端

```
宿主 ──stdio──> mcp_server.py ──HTTP──> 127.0.0.1:7788 (常驻服务，跑模型)
```

**为什么不在 MCP 进程里直接跑模型**：CUDA 后端下，每个新进程的首次卷积固定要
**~35 秒**（cuDNN 要为 113 个卷积层逐一枚举算法，8GB 卡上换 `EXHAUSTIVE` /
`HEURISTIC`、开关 `cudnn_conv_use_max_workspace` 都绕不过）。宿主会反复拉起
MCP 进程，每次都吃半分钟，工具调用的超时都撑不到。

所以推理在常驻服务里做一次、一直活着；MCP 进程只管转发，启动是毫秒级。
服务没起来时 MCP 会自动把它拉起来（CUDA 首次约 35 秒、DirectML 约 2 秒），
之后就都是几十到几百毫秒。

DirectML 后端没有那个 35 秒问题（建会话 1.5 秒、首次推理直接就是稳态速度），
但常驻的结构照旧 —— 换后端不该动架构，而且常驻还省掉了每次调用重读 77MB 模型的 IO。

常驻服务用 `DETACHED_PROCESS` 启动，**独立于 MCP 进程的生命周期**：宿主重启
MCP 不会杀掉预热好的服务，下次直接可用。

## 边界：只定位，不点击

定位完拿到坐标，**用 `cua_driver_native__click` 去点**，不要在这里自己调
`SendInput` / `SetCursorPos`。

原因不是设计偏好，是实测：本服务进程调这两个 API **会静默失败**——
返回成功、`GetLastError()` 为 0、光标一步都不动（连续 6 次命中 0/6）。
而同一台机器上 `cua_driver` 的桌面点击一直正常。查过进程的窗口站与桌面
（`WinSta0` / `Default` / input desktop 也是 `Default`，会话同为 1），
没有隔离迹象，也没找到 HIPS 拦截记录。**根因未定位**，但两边的行为
稳定可复现：定位走本服务，输入走 cua_driver。

完整链路实测（2026-10-23）：

```
POST /locate {"text":"编辑"}  →  {"ok":true,"x":128,"y":20,"matched":"编辑"}
cua_driver_native__click(scope="desktop", x=128, y=20)  →  菜单精确展开
```

`cua_driver_native__click` 不能同时传 `target` 和 `scope`（报
`target cannot be combined with legacy scope, pid, or window_id fields`），
桌面点击用 `scope="desktop"` + x/y 即可。

## 环境

不需要改任何源码来适配机器——下面这些都由 `src/ui_locate/config.py` 在运行时探测：

- **推理后端**：按 `UI_LOCATE_PROVIDER` 走，默认 `auto` —— 装的是 CUDA 版
  ONNX Runtime 就用 `CUDAExecutionProvider`，否则有 `DmlExecutionProvider`
  就用它（Windows 默认安装走这条，AMD / Intel 显卡都能用），再没有就退回 CPU。
  想固定可以设成 `cuda` / `dml` / `cpu`。
- **检测模型**：默认文件名 `omniparser_icon_detect_yolo11m.onnx`（80.3 MB，YOLO11-medium），
  按 `UI_LOCATE_MODEL` → `UI_LOCATE_MODEL_DIR` → 状态目录 `models/` → 几个常见位置
  的顺序查找；全都没有时报错并给出下载地址。
- **OCR**：RapidOCR 3.x，模型随包，**固定走 CPU**（GPU 上反而慢 5 倍，见性能章节）。
- **Python**：3.12。`onnxruntime-gpu` 的 wheel 只发到 cp313，3.14 装不上。

参考实测（8GB 显存的笔记本独显）：单块检测 **DirectML 10.1 ms / CUDA 10.8 ms / CPU 176 ms**，
整屏定位 0.5~2 秒。

## 安装

### 方式一：从插件市场在线导入（推荐）

本包自带 `market-index.json`，是一份符合 DSH **插件市场索引格式 1** 的清单。
把它放到任意 https 静态地址（GitHub Pages、对象存储、内网 nginx 都行），
再在 DSH 里添加该地址即可：

1. **插件** → **发现** → **添加索引源**，填索引 JSON 的 https 地址。
2. 列表里出现「屏幕定位 (ui-locate)」，点安装。
3. 装完后先跑一次环境准备（venv + 依赖 + 模型，一次性）：

```powershell
pwsh -File <插件目录>\scripts\bootstrap.ps1
```

这一步会往**用户级目录**写东西，不在插件目录里，因为 bundle 装在
`node_modules` 深处、换版本时整目录会被替换。位置由下面几条决定（先命中者胜）：

| 东西 | 大小 | 位置 | 怎么改 |
|------|------|------|--------|
| 代码 + 补丁 | 约 500 KB | profile 的 `node_modules/ui-locate-mcp/` | DSH 管，别动 |
| venv + 依赖 | **DirectML 约 130 MB / CUDA 约 1.8 GB / 纯 CPU 约 60 MB** | `$UI_LOCATE_HOME/venv` | `UI_LOCATE_HOME` 或 `-HomeDir` |
| 检测模型 | 77 MB | `$UI_LOCATE_MODEL_DIR` | `UI_LOCATE_MODEL_DIR` 或 `-ModelDir` |
| 日志 | 几十 KB | `$UI_LOCATE_HOME/` | 同上 |

**不要塞进包里**：venv 光 CUDA 运行库就有 1.5 GB，随包下发意味着每次装插件
都得拖一份 GB 级的东西。所以「装插件」和「准备环境」是两步。

**默认装的是 DirectML 后端**（`onnxruntime-directml` 一个 wheel 24 MB，自带
`DirectML.dll`，不依赖系统里装没装 DirectML）。要 CUDA 就加 `-Cuda`，要纯
CPU 就加 `-CpuOnly`。两个都别加就是 DirectML。

⚠ 三个 ORT wheel（`onnxruntime` / `onnxruntime-directml` / `onnxruntime-gpu`）
**都提供同一个 `onnxruntime` 顶层包**，装两个就互相覆盖 —— 后装的赢，而且不报错。
所以换后端别在现有 venv 上叠着装，删掉 venv 重跑 `bootstrap.ps1`。

```powershell
pwsh -File <插件目录>\scriptsootstrap.ps1 -Cuda      # NVIDIA 卡，约 1.8 GB
pwsh -File <插件目录>\scriptsootstrap.ps1 -CpuOnly   # 没有独显 / 只想省空间
```

**已经有模型的话别下第二份**。脚本按「先找现成的、找不到才下」走：

```powershell
# 指向已有的模型目录（或直接给文件的绝对路径用 UI_LOCATE_MODEL）
pwsh -File <插件目录>\scripts\bootstrap.ps1 -ModelDir D:\myLocalModels\ui-locate
```

模型查找顺序是 `UI_LOCATE_MODEL` → `UI_LOCATE_MODEL_DIR` → 状态目录 `models/`
→ 包内 `models/` → `~/myLocalModels/ui-locate` → 当前目录 `models/`。
`bootstrap.ps1 -Status` 会把实际用的 `home` / `models` / `venv` / `model` 全打出来。

   实际路径取决于插件被装到哪里；也可以直接 `cd` 到插件目录后跑
   `pwsh -File .\scripts\bootstrap.ps1`。

4. 重启 DSH（`restart-required` 是 bundle 生效的正常状态）。

**为什么索引里写的是 npm、而实际要跑 bootstrap**：索引只描述「怎么把包拉下来」，
拉下来的是**源码 + patch**；模型和 CUDA 依赖体积大、与平台强相关，不适合塞进 npm 包，
所以交给 `bootstrap.ps1` 按需准备。这是刻意分工，不是遗漏。

### 方式二：手写进 profile（本地开发用）

```powershell
cd <插件目录>
pwsh -File .\scripts\bootstrap.ps1 -Editable   # 装成 editable，改代码即生效
```

然后在当前 profile 的 `cordis.patch.yml` 里注册：

```yaml
- insert:
    - id: mcp-ui-locate
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: ui-locate
        transport: stdio
        command: 'D:\path\to\ui-locate\venv\Scripts\python.exe'
        args: [ '-m', 'ui_locate.mcp_server' ]
        env:
          UI_LOCATE_HOME: 'D:\path\to\ui-locate'
        toolCallTimeoutMs: 180000
```

**注意 `command` 必须是绝对路径**（DSH 的 stdio 分支会校验 `isAbsolute`），
且 `-m ui_locate.mcp_server` 依赖包已装进那个 venv —— 这正是 `bootstrap.ps1` 干的事。

bundle 形态下 `cordis.patch.yml` 里的 command 用 `!!js` 现算，目录按
`UI_LOCATE_HOME` → `DSH_HOME/ui-locate` → `%LOCALAPPDATA%/ui-locate` 的顺序挑：

```yaml
command: !!js '`${process.env.UI_LOCATE_HOME ?? (process.env.DSH_HOME ? `${process.env.DSH_HOME}/ui-locate` : (process.env.LOCALAPPDATA ?? process.env.HOME) + "/ui-locate")}/venv/Scripts/python.exe`'
env:
  UI_LOCATE_HOME: !!js '`${process.env.UI_LOCATE_HOME ?? (process.env.DSH_HOME ? `${process.env.DSH_HOME}/ui-locate` : (process.env.LOCALAPPDATA ?? process.env.HOME) + "/ui-locate")}`'
```

因为 bundle 会被 pnpm 装进 `node_modules` 深处的随机目录，包内 `.venv` 的绝对路径
没法在 patch 里写死，所以运行环境统一放在用户级固定位置。

`!!js` 表达式在 **Host 进程**里求值，能读到 `DSH_HOME`；但 MCP 子进程的环境会被
scrub 掉所有 `DSH_` 开头的变量，所以还要把算好的目录经 `config.env` 显式传下去
（`env` 是 scrub 之后的合并层，能穿透）。

### 环境变量

全部可选，都有默认值：

| 变量 | 作用 | 默认 |
|------|------|------|
| `UI_LOCATE_MODEL` | 模型 onnx 绝对路径 | 自动查找 |
| `UI_LOCATE_MODEL_DIR` | 模型目录（`bootstrap.ps1 -ModelDir` 对应它） | 状态目录 `models/` |
| `UI_LOCATE_HOME` | 状态目录（模型 / 日志） | `$DSH_HOME/ui-locate`，再退回 `%LOCALAPPDATA%\ui-locate` |
| `UI_LOCATE_HOST` / `UI_LOCATE_PORT` | 服务监听 | `127.0.0.1` / `7788` |
| `UI_LOCATE_BASE` | 客户端连接的地址 | `http://<host>:<port>` |
| `UI_LOCATE_PROVIDER` | `auto` / `cuda` / `dml` / `cpu` | `auto` |
| `UI_LOCATE_MIN_EDGE` | 跳过空白块的边缘密度下限 | `0.0008` |
| `UI_LOCATE_MAX_SIDE` | 分块时每块长边 | `960` |
| `UI_LOCATE_OCR_CUDA` | `1` 让 OCR 也走 GPU | 关 |
| `DSH_UI_LOCATE_UPDATE_API` | 换掉查版本的 GitHub API 地址（自建镜像 / 离线测试） | `https://api.github.com` |
| `DSH_UI_LOCATE_UPDATE_DELAY_MS` | 启动后隔多久查第一次 | `8000` |

## 自动更新

包内有一个 Host 插件（`plugin/index.mjs`），负责**查版本、装版本、同步 Python 侧**。
它注册了工具 `ui_locate_update`：

```
ui_locate_update(action: "check")   # 只读发布标签，报版本差异
ui_locate_update(action: "apply")   # 装最新版本（会改动本机插件）
```

### 为什么需要一段 Host 代码

这个 bundle 的主体是一个 MCP 客户端行，它只声明「用什么命令起 Python 进程」，
本身没有可执行代码。DSH 的插件管理器能装 JS/YAML 那部分，但**装不到 venv 里的
Python 包**——而那才是真正会改进的部分（算法、模型、OCR 参数）。

### 更新是两段，顺序不能颠倒

1. **JS 侧**：`pluginManager.installBundle('git+<url>#vX.Y.Z')`，DSH 用它装官方
   插件的方式装本包，落到 profile 的 `node_modules`，然后热替换本插件。
2. **Python 侧**：新版本的源码这时才出现在 `node_modules` 里，所以只能在**换上
   来之后**执行。它由「新实例启动时发现版本不一致」触发，不塞进 install 的同一
   段流程——那样会拿旧路径去装新版本。

这个顺序也给了容错：Python 侧失败不影响 JS 侧已经装好，下次启动会重新发现不一致
并再试一次，不需要人干预。

### 只查不装

`updateCheck` 默认开着，但**检查本身从不安装任何东西**——它只读发布标签并记下
结论。安装必须由一次显式的工具调用触发。

### 发布新版本时要做什么

两处版本号必须一起改，否则每次启动都会白装一次 Python 侧：

- `package.json` 的 `version`
- `pyproject.toml` 的 `[project] version`

然后打一个 `vX.Y.Z` 标签（只认这三段，预发布标签会被忽略）+ 推上去：

```bash
git tag v0.2.1 && git push gh main --tags
```

### 两个开关

在插件页的配置里：

| 开关 | 默认 | 作用 |
|------|------|------|
| `updateCheck` | 开 | 启动几秒后查一次，之后每 12 小时一次 |
| `pythonSync` | 开 | 发现 venv 版本与包内声明不一致时自动重装 |

### 自检

```powershell
# 结构自检（不启动任何东西，纯静态核对 bundle 规范）
python tests\check_bundle.py

# 全链路冒烟（真实 stdio JSON-RPC，8 项）
python tests\smoke_mcp.py

# 更新插件的行为测试（48 例，纯逻辑 + 接线层）
node --test tests/
```

`smoke_mcp.py` 的解释器优先级：`UI_LOCATE_PYTHON` 环境变量 → 包内 `.venv` → 当前解释器。

## 直接起停服务（可选）

正常使用不必手动跑——MCP 工具会自己拉起。这个脚本是给「想单独起、单独停、
单独看日志」的场合：

```powershell
pwsh -File .\scripts\serve.ps1          # 启动
pwsh -File .\scripts\serve.ps1 -Stop    # 停止
pwsh -File .\scripts\serve.ps1 -Status  # 看进程、内存、conhost、屏幕几何
```

日志在 `.cache\server.log` / `.cache\server.err.log`。

服务用 **`pythonw.exe`** 启动（不是 `python.exe`），所以**不会留下常驻的 CMD 窗口**，
`-Status` 里的 `conhost` 一栏应该是「无」。原因见坑 11。

## HTTP 接口

服务也可直接调（端口 7788）：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 存活、预热状态、屏幕几何 |
| GET | `/monitors` | 所有显示器的尺寸与原点（副屏原点是负数） |
| POST | `/detect` | body 为 `{"path": "..."}`、原始图片字节，或 `{}` 表示抓当前屏 |
| POST | `/locate` | body `{"text": "保存"}`，返回该元素中心坐标（**只给坐标，不点击**） |
| GET | `/find?text=保存` | 抓屏 + 检测 + 按文字查 |

参数：`conf`（默认 0.3）、`iou`（0.5）、`tiled`（0/1）、`max_side`（960）、
`text`（0/1，是否跑 OCR）、`text_mode`（`full` / `mosaic`）、
`monitor`（`all` / `primary` / 索引）、`smart`（0/1，智能分割）。
**参数放 query 或 JSON body 都可以，body 优先**。

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
8. **别在本服务进程里调 `SendInput` / `SetCursorPos`**。会静默失败：返回成功、
   错误码 0、光标不动（实测 0/6）。点击统一交给 `cua_driver_native__click`。
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

## 文件

| 文件 | 作用 |
|------|------|
| `package.json` | **bundle 声明**：`dsh.bundle.patch` 指向 `cordis.patch.yml`，`exports` 暴露 locale 与图标 |
| `cordis.patch.yml` | bundle 的 patch：`insert` 一个 `@deepseek-ai/dsh-mcp-client` 连接项 + 一个本包 Host 插件行 |
| `plugin/index.mjs` | **Host 插件**：版本检查、安装新版本、把 venv 里的 Python 包同步过去 |
| `plugin/tool.mjs` | 本仓库自带的 `defineTool`：宿主那几条会让插件起不来的硬规则都在这里 |
| `plugin/core.mjs` | 上者的纯逻辑（版本比较、发布源、venv 同步、状态机），不依赖 Cordis，可直接用 node 测 |
| `market-index.json` | 插件市场索引（format 1），在线导入的入口 |
| `locale/{en,zh}.json` | 插件列表里显示的标题与描述 |
| `icon.svg` | 插件图标（≤256 KiB，必须留在包内） |
| `src/ui_locate/mcp_server.py` | **MCP 入口**（FastMCP + stdio），薄客户端 + 自动拉起服务 |
| `src/ui_locate/config.py` | **运行期配置层**：模型路径、端口、provider、各类阈值的唯一来源 |
| `src/ui_locate/server.py` | 常驻 HTTP 服务，跑模型 |
| `src/ui_locate/screen.py` | **屏幕几何层**：DPI 感知、枚举显示器、按屏抓屏（唯一坐标权威） |
| `src/ui_locate/detect.py` | 单块检测核心，letterbox + NMS + 反变换 |
| `src/ui_locate/dml_fix.py` | **DirectML 兼容层**：改写导出模型里的 `allowzero`（详见坑 20） |
| `src/ui_locate/tiled.py` | 分块检测（含智能分割：切缝对齐 + 跳空白块） |
| `src/ui_locate/ocr_match.py` | 整页 OCR 与检测框做空间匹配、按文字查找、图标噪声剥离 |
| `src/ui_locate/mosaic.py` | 裁片拼图 OCR（快 4 倍的路线） |
| `src/ui_locate/annotate.py` | 把检出的元素裁片拼成带编号的总览图 |
| `src/ui_locate/locate.py` | 抓屏包装 + OCR |
| `src/ui_locate/client.py` | HTTP 客户端 + CLI |
| `src/ui_locate/cuda_dlls.py` | 注册 nvidia 包里的 CUDA dll 目录 |
| `scripts/bootstrap.ps1` | **一次性环境准备**：建 venv、装依赖、下模型 |
| `scripts/make_index.py` | 生成 / 校验市场索引 JSON |
| `scripts/serve.ps1` | 手动起停常驻服务 / 查状态 |
| `tests/check_bundle.py` | bundle 结构静态自检（对照 DSH 的校验规则） |
| `tests/smoke_mcp.py` | stdio 全链路冒烟测试（8 项） |
| `tests/update-plugin.test.mjs` | 更新插件的行为测试（48 例，`node --test tests/`） |
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

## 一次失败的尝试，留个记录

曾经想在服务里顺手把点击也做了（用 `SendInput` 自己注入），失败。原因是本机
Python 进程调输入 API 会静默失败（见坑 8）。教训：**定位和输入是两件事，
各有各的强手**——定位这份活儿归 YOLO+OCR，输入归 `cua_driver`。
硬要在一个进程里全包，就是在别人的地盘上打自己的仗。

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
