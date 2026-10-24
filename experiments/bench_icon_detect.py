r"""OmniParser icon_detect (YOLO11m) 多后端延迟基准。

用法（在工程根目录）：
  # 先设置 PYTHONPATH 指向本工程的 src 目录
  .\.venv\Scripts\python.exe experiments\bench_icon_detect.py --providers cpu cuda
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import config, cuda_dlls

cuda_dlls.register()

import onnxruntime as ort  # noqa: E402  (必须在 DLL 目录注册之后导入)

MODEL = config.model_path()
SAMPLE = Path(os.environ.get("UI_LOCATE_SAMPLE", "sample.png"))
INPUT_SIZE = 640
WARMUP = 3
RUNS = 30

PROVIDER_ALIASES = {
    "cpu": "CPUExecutionProvider",
    "cuda": "CUDAExecutionProvider",
    "trt": "TensorrtExecutionProvider",
}


def load_input(src: Path) -> np.ndarray:
    im = Image.open(src).convert("RGB").resize((INPUT_SIZE, INPUT_SIZE), Image.BILINEAR)
    arr = np.asarray(im, dtype=np.float32) / 255.0
    return np.ascontiguousarray(arr.transpose(2, 0, 1)[None, ...])


def make_session(model: Path, provider: str, threads: int | None = None) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.log_severity_level = 3
    if threads is not None:
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
    opts: dict = {}
    if provider == "TensorrtExecutionProvider":
        # TensorRT 引擎缓存是可重建的产物，不进模型目录（模型目录只读存放权重）
        cache = Path(__file__).resolve().parents[1] / ".cache" / "trt"
        cache.mkdir(parents=True, exist_ok=True)
        opts = {
            "trt_fp16_enable": True,
            "trt_engine_cache_enable": True,
            "trt_engine_cache_path": str(cache),
            "trt_timing_cache_enable": True,
        }
    return ort.InferenceSession(str(model), so, providers=[(provider, opts)])


def bench(sess: ort.InferenceSession, x: np.ndarray) -> float:
    iname = sess.get_inputs()[0].name
    onames = [o.name for o in sess.get_outputs()]
    for _ in range(WARMUP):
        sess.run(onames, {iname: x})
    t0 = time.perf_counter()
    for _ in range(RUNS):
        sess.run(onames, {iname: x})
    return (time.perf_counter() - t0) / RUNS * 1000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", nargs="+", default=["cpu", "cuda"])
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--model", type=Path, default=MODEL)
    args = ap.parse_args()

    x = load_input(SAMPLE)
    print(f"ort    : {ort.__version__}")
    print(f"model  : {args.model.name} ({args.model.stat().st_size / 1e6:.1f} MB)")
    print(f"input  : {x.shape} {x.dtype}")
    print(f"warmup={WARMUP} runs={RUNS}\n")

    for key in args.providers:
        prov = PROVIDER_ALIASES[key]
        try:
            sess = make_session(args.model, prov, args.threads)
            actual = sess.get_providers()[0]
            ms = bench(sess, x)
            tag = "  [FELL BACK TO CPU]" if actual != prov else ""
            print(f"{key:5s} -> {actual:32s} {ms:8.2f} ms  {1000 / ms:6.1f} FPS{tag}")
        except Exception as e:  # noqa: BLE001
            print(f"{key:5s} -> FAILED  {type(e).__name__}: {str(e)[:160]}")


if __name__ == "__main__":
    main()
