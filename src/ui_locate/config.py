"""运行期配置解析 —— 所有「环境相关」的值都从这里取。

设计原则：模块里**不写死任何本机路径、屏幕尺寸、显卡型号**。
默认值只用于推导，推导不出来就给一条能照着做的错误信息。

模型路径解析顺序（先命中者胜）：
  1. 环境变量 ``UI_LOCATE_MODEL``        —— 直接给 onnx 文件的绝对路径
  2. 环境变量 ``UI_LOCATE_MODEL_DIR``    —— 给目录，拼上默认文件名
  3. 包目录下的 ``models/``
  4. 几个常见模型根目录（见 ``_default_model_dirs``）
  5. 都没有 → ``ModelNotFound``，附带下载指引

环境变量一览：
  UI_LOCATE_HOME        状态目录根（模型 / 日志），见 ``state_dir``
  UI_LOCATE_MODEL       模型 onnx 绝对路径
  UI_LOCATE_MODEL_DIR   模型目录
  UI_LOCATE_HOST        服务监听地址，默认 127.0.0.1
  UI_LOCATE_PORT        服务端口，默认 7788
  UI_LOCATE_BASE        客户端连接的服务地址，默认 http://<host>:<port>
  UI_LOCATE_PROVIDER     cuda / dml / cpu / auto，默认 auto（cuda → dml → cpu）

可调参数（都有默认值，一般不用改）：
  UI_LOCATE_MIN_EDGE     跳过空白块的边缘密度下限，默认 0.0008
  UI_LOCATE_MAX_SIDE     分块时每块长边，默认 960
  UI_LOCATE_OCR_CUDA     "1" 让 OCR 也走 GPU（多数情况更慢，见 README）
"""

from __future__ import annotations

import os
from pathlib import Path

MODEL_FILENAME = "omniparser_icon_detect_yolo11m.onnx"

# 模型来源：HuggingFace `onnx-community/OmniParser-v2.0_icon_detect`
# （YOLO11-medium 单类图标检测头，输入 [1,3,640,640]，输出 [1,5,8400]）
MODEL_SOURCE = "https://huggingface.co/onnx-community/OmniParser-v2.0_icon_detect"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7788
DEFAULT_PROVIDER = "auto"

PACKAGE_ROOT = Path(__file__).resolve().parent


def state_dir() -> Path:
    """运行期状态目录：模型、日志、venv 之外的所有产物都落这里。

    为什么不放包目录：bundle 装进 node_modules 后，那份目录可能只读，
    换版本时还会被整个替换 —— 模型和日志跟着丢。放用户级目录才稳。

    解析顺序（先命中者胜）：

    1. ``UI_LOCATE_HOME`` —— 显式指定，最优先。DSH 组合包的 ``cordis.patch.yml``
       会把算好的目录从这里传进来，所以装成插件时走的就是这条。
    2. ``DSH_HOME/ui-locate`` —— 便携版 DSH 把数据放在自己的 data 目录下，
       跟着走就不会往系统盘塞东西。注意：MCP 子进程的环境会被 scrub 掉所有
       ``DSH_`` 开头的变量，所以插件运行时这条通常读不到，靠第 1 条兜住；
       这条对「手动跑 server、自己设了 DSH_HOME」的场景有效。
    3. ``%LOCALAPPDATA%/ui-locate`` —— 普通 Windows 安装的兜底。
    4. ``$XDG_CACHE_HOME/ui-locate`` 或 ``~/.local/share/ui-locate`` —— 其余平台。
    """
    for env_name in ("UI_LOCATE_HOME", "DSH_HOME"):
        root = os.environ.get(env_name)
        if root:
            base = Path(root).expanduser()
            # DSH_HOME 是 DSH 的数据根，插件自己的东西要放进它的子目录
            return base if env_name == "UI_LOCATE_HOME" else base / "ui-locate"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "ui-locate"
    return Path.home() / ".local" / "share" / "ui-locate"


class ModelNotFound(RuntimeError):
    """模型文件找不到时抛出，信息里带能照着做的指引。"""


def _default_model_dirs() -> list[Path]:
    """常见的模型存放位置。顺序即优先级，全部不存在也不报错。"""
    cands: list[Path] = []
    env_dir = os.environ.get("UI_LOCATE_MODEL_DIR")
    if env_dir:
        cands.append(Path(env_dir).expanduser())
    cands.extend(
        [
            state_dir() / "models",
            PACKAGE_ROOT / "models",
            Path.home() / "myLocalModels" / "ui-locate",
            Path.cwd() / "models",
        ]
    )
    return cands


def model_path(explicit: str | Path | None = None) -> Path:
    """解析检测模型文件路径。

    Args:
        explicit: 显式指定的路径（命令行 ``--model`` 用）。给了就必须存在。

    Raises:
        ModelNotFound: 所有候选位置都没有模型文件。
    """
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise ModelNotFound(f"指定的模型文件不存在：{p}")
        return p

    env = os.environ.get("UI_LOCATE_MODEL")
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p
        raise ModelNotFound(f"环境变量 UI_LOCATE_MODEL 指向的文件不存在：{p}")

    for d in _default_model_dirs():
        p = d / MODEL_FILENAME
        if p.is_file():
            return p

    searched = "\n".join(f"  - {d}" for d in _default_model_dirs())
    raise ModelNotFound(
        f"找不到检测模型 {MODEL_FILENAME}\n"
        "已查找以下位置：\n"
        f"{searched}\n\n"
        "解决办法（任选其一）：\n"
        f"  1. 从 {MODEL_SOURCE} 下载 {MODEL_FILENAME}，"
        "放到上面任意一个目录\n"
        "  2. 设置环境变量 UI_LOCATE_MODEL 为文件的绝对路径\n"
        "  3. 启动时传 --model <绝对路径>"
    )


def host() -> str:
    return os.environ.get("UI_LOCATE_HOST", DEFAULT_HOST)


def port() -> int:
    try:
        return int(os.environ.get("UI_LOCATE_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def base_url() -> str:
    return os.environ.get("UI_LOCATE_BASE", f"http://{host()}:{port()}")


def provider() -> str:
    return os.environ.get("UI_LOCATE_PROVIDER", DEFAULT_PROVIDER)


def resolve_provider(want: str | None = None) -> str:
    """把 ``auto`` 解析成真正能用的 provider，返回 ``cuda`` / ``dml`` / ``cpu``。

    判定依据是「ONNX Runtime 是否真的列出那个 ExecutionProvider」，而不是猜
    显卡型号 —— 装了 GPU 版 ORT 但驱动/库不全时，provider 列表里就不会有它，
    这时硬上只会得到一个含义模糊的加载错误。

    ``auto`` 的偏好顺序是 **cuda → dml → cpu**：

    * CUDA 最快，但只有 NVIDIA 卡能用，且要带 1.8 GB 的 cudnn/cublas。
    * DirectML 走 D3D12，AMD / Intel / NVIDIA 通吃，实测速度与 CUDA 基本
      持平（10.26 ms vs 10.80 ms），依赖只有 24 MB。没有 NVIDIA 卡时它是最
      划算的选择。
    * CPU 兜底。

    注意 DirectML 的模型兼容性坑：它不支持 opset 14 的 ``allowzero`` 属性，
    遇到时 ONNX Runtime **不抛异常而是静默回退 CPU**，看起来「能跑只是慢」。
    ``Detector`` 会在建会话前把该属性改掉（见 ``dml_fix``），所以这里不用管。
    """
    want = (want or provider()).lower()
    if want in ("cpu", "cuda", "dml"):
        return want
    try:
        import onnxruntime as ort

        available = ort.get_available_providers()
    except Exception:
        return "cpu"
    if "CUDAExecutionProvider" in available:
        return "cuda"
    if "DmlExecutionProvider" in available:
        return "dml"
    return "cpu"


def provider_chain(name: str) -> list:
    """把一个 provider 名翻成 ONNX Runtime 认的 providers 列表（含 CPU 兜底）。

    显式列出 ``CPUExecutionProvider`` 是必须的：不列的话 ORT 在 GPU provider
    不可用时会直接报错，而不是降级。
    """
    if name == "cuda":
        return [("CUDAExecutionProvider", dict(CUDA_OPTS)), "CPUExecutionProvider"]
    if name == "dml":
        # device_id 不指定时 ORT 按「高性能」偏好自选适配器 —— 在多显卡机器上
        # 这会选到独显而不是核显，正是我们想要的行为。
        return [("DmlExecutionProvider", dict(DML_OPTS)), "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


# cuDNN 调优参数（实测数据，见 README 的性能章节）：
#   EXHAUSTIVE + max_workspace=1 → 首次 34306ms（8GB 显存放不下 workspace 表，反复重算）
#   EXHAUSTIVE + max_workspace=0 → 首次  1035ms，稳态 10.80ms  ← 采用
#   HEURISTIC  + max_workspace=1 → 首次  1014ms，稳态 11.71ms
# 根因：max_workspace=1 时 cuDNN 尝试为每层保留最大工作区，8GB 卡上大量层
# 回退到 Fallback 模式（"May be extremely slow"），首帧反复试算。
CUDA_OPTS = {
    "cudnn_conv_algo_search": "EXHAUSTIVE",
    "cudnn_conv_use_max_workspace": "0",
    "do_copy_in_default_stream": "1",
}

# DirectML 没有对应 cuDNN 的调优项。enable_mem_pattern 默认为真，保持不动；
# 这里留一个空字典是为了让两边的写法对称，将来要加参数也有地方放。
DML_OPTS: dict = {}


def min_edge_ratio() -> float:
    """跳过空白块的边缘密度下限。

    这个量是「边缘像素占该块总像素的比例」，与分辨率无关，因此同一个值
    在不同屏幕上含义一致。默认 0.0008 的来历：纯色背景块实测约 0.0000，
    带一行小字的块约 0.004，满屏文本约 0.05 —— 取比「一行小字」低 5 倍的
    值，宁可多扫一块，也不漏掉稀疏的小按钮。
    """
    try:
        return float(os.environ.get("UI_LOCATE_MIN_EDGE", 0.0008))
    except ValueError:
        return 0.0008


def max_side() -> int:
    """分块时每块的长边目标。默认 960：640 会把中等图标切碎，
    1280 时块数变少但小元素召回下降。"""
    try:
        return int(os.environ.get("UI_LOCATE_MAX_SIDE", 960))
    except ValueError:
        return 960


def ocr_on_cuda() -> bool:
    return os.environ.get("UI_LOCATE_OCR_CUDA", "") in ("1", "true", "True", "yes")
