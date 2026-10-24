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
  UI_LOCATE_MODEL       模型 onnx 绝对路径
  UI_LOCATE_MODEL_DIR   模型目录
  UI_LOCATE_HOST        服务监听地址，默认 127.0.0.1
  UI_LOCATE_PORT        服务端口，默认 7788
  UI_LOCATE_BASE        客户端连接的服务地址，默认 http://<host>:<port>
  UI_LOCATE_PROVIDER     cuda / cpu / auto，默认 auto

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
DEFAULT_PROVIDER = "cuda"

PACKAGE_ROOT = Path(__file__).resolve().parent


def state_dir() -> Path:
    """运行期状态目录：模型、日志、venv 之外的所有产物都落这里。

    为什么不放包目录：bundle 装进 node_modules 后，那份目录可能只读，
    换版本时还会被整个替换 —— 模型和日志跟着丢。放用户级目录才稳。
    """
    root = os.environ.get("UI_LOCATE_HOME")
    if root:
        return Path(root).expanduser()
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
    return os.environ.get("UI_LOCATE_PROVIDER", "auto")


def resolve_provider(want: str | None = None) -> str:
    """把 ``auto`` 解析成真正能用的 provider。

    判定依据是「ONNX Runtime 是否真的列出 CUDAExecutionProvider」，
    而不是猜显卡型号 —— 装了 CUDA 版 ORT 但驱动/库不全时，provider
    列表里就不会有它，这时硬上 CUDA 只会得到一个含义模糊的加载错误。
    """
    want = (want or provider()).lower()
    if want in ("cpu", "cuda"):
        return want
    try:
        import onnxruntime as ort

        return "cuda" if "CUDAExecutionProvider" in ort.get_available_providers() else "cpu"
    except Exception:
        return "cpu"


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
