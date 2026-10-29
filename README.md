# ui-locate-mcp

简体中文 · [English](README.en.md) · [npm](https://www.npmjs.com/package/ui-locate-mcp)

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
| `ui_locate_act` | 定位后执行点击、输入、滚动等动作，并尝试验证 |
| `ui_locate_steps` | 串行执行一组步骤，返回失败位置 |
| `ui_locate_frames` / `ui_locate_frame` | 列出或取回近期动作帧 |
| `list_monitors` | 显示器尺寸、原点和索引 |
| `health` | 检查服务是否就绪 |
| `ui_locate_setup` | 准备或重建 Python 环境与模型 |
| `ui_locate_update` | 检查或安装本插件的 npm 更新 |

前九项由 MCP 服务提供，最后两项由 Host 插件提供；实际调用名称可能带宿主添加的命名空间。

~~~python
locate_text(text="保存", monitor="primary")
ui_locate_act(text="保存", action="click", monitor="primary")
~~~

坐标使用屏幕绝对位置，多显示器可能返回负坐标。`monitor` 可为 `primary`、索引字符串或 `all`。界面移动后应重新定位，不要长期复用旧坐标。

## 配置与数据目录

Host 行 ID 是 `ui-locate-update`，MCP 行 ID 是 `mcp-ui-locate`。

| Host 配置 | 默认 | 说明 |
| --- | --- | --- |
| `updateCheck` | `true` | 定时检查 npm；不会因此自动安装 JS bundle |
| `pythonSync` | `true` | 将环境中的 Python 包同步到 bundle 声明的版本 |
| `runtimeDir` | 空 | Python 环境、状态与日志目录 |
| `modelDir` | 空 | 模型目录，可复用已有模型 |
| `backend` | 空 | 沿用已保存选择；首次默认 DirectML |

默认运行目录按 `UI_LOCATE_HOME` → `DSH_HOME/ui-locate` → `LOCALAPPDATA/ui-locate` 选择。目录留空可跟随便携版的数据目录；显式绝对路径需要自行维护。

Python 服务还识别 `UI_LOCATE_MODEL`、`UI_LOCATE_MODEL_DIR`、`UI_LOCATE_PROVIDER` 和 `UI_LOCATE_PORT`，详见 [config.py](src/ui_locate/config.py)。MCP 默认连接本机 7788 端口。

## 更新

**当前代码仍保留独立更新器，更新来源已经改为 npm。** `updateCheck` 默认约 8 秒后检查，之后每 12 小时检查一次；安装由界面或 `ui_locate_update(action="apply")` 发起，`action="check"` 只检查。

更新先由 DSH 安装 npm bundle，再由新实例按 `pythonSync` 同步 Python 包。这与首次准备环境是不同步骤。关闭 `updateCheck` 可以停用定时检查，但不等于移除了更新工具。入口核对见[更新机制审计](docs/update-audit.md)。

## 排障与使用边界

- MCP 启动失败：先检查运行环境是否准备完成、虚拟环境 Python 是否存在，再重连 MCP。
- 动作结果中的 `ok` 表示执行流程完成；只有对应的验证结果才能说明界面是否变化。`verified: false` 或 `null` 时不要当作操作成功。
- 桌面权限、前台焦点、DPI 和应用框架会影响输入与截图。定位命中也不保证目标接受合成输入。
- 不同 ONNX Runtime 后端共用同名 Python 包；换后端应重建，避免叠装。
- OCR 有漏检和误识别；重要动作后检查画面或目标文字。

## 开发与参考

~~~sh
python tests/check_bundle.py
node --test tests/
python -m unittest discover -s tests -p "test_*.py"
~~~

Python 测试需要相应依赖；真实模型和桌面冒烟还需要准备好的环境。历史性能与算法排障记录已移到[技术笔记](docs/technical-notes.md)，不作为当前机器的性能承诺。

当前 [release.yml](.github/workflows/release.yml) 按版本标签或手动指定标签发布 npm。发布时需保持 package.json 与 pyproject.toml 的版本一致。

## 许可证

[MIT](LICENSE)
