"""让 onnxruntime-gpu 能找到 nvidia pip wheel 里的 CUDA 运行时 DLL。

Python 3.8+ 起不再用 PATH 搜索 DLL（add_dll_directory 只对 LoadLibrary 生效），
而 onnxruntime 的 provider bridge 用的是带 ALTERED_SEARCH_PATH 的加载方式，
因此必须把 nvidia/*/bin 同时加入 os.add_dll_directory 和 os.environ["PATH"]。

这些 wheel 都是 pip 可安装的 nvidia-cuda-*-cu12 系列，不依赖系统 CUDA Toolkit。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REGISTERED: list[Path] = []


def register() -> list[Path]:
    """把 site-packages/nvidia/*/bin 加入 DLL 搜索路径，返回注册过的目录。"""
    if _REGISTERED:
        return _REGISTERED

    nvidia_root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not nvidia_root.is_dir():
        return _REGISTERED

    bins = [p / "bin" for p in sorted(nvidia_root.iterdir()) if (p / "bin").is_dir()]
    for bin_dir in bins:
        _REGISTERED.append(bin_dir)
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(bin_dir))

    # provider bridge 需要 PATH，否则 cudart64_12.dll 之类仍找不到
    existing = os.environ.get("PATH", "")
    prefix = os.pathsep.join(str(b) for b in bins)
    if prefix not in existing:
        os.environ["PATH"] = prefix + os.pathsep + existing

    return _REGISTERED


if __name__ == "__main__":
    for d in register():
        print(d)
