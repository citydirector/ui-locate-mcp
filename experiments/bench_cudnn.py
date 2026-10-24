"""cuDNN 算法搜索配置矩阵：找首次推理最快且稳态最快的组合。

结论（已实测，别再重复试）：新进程首次 CUDA 推理固定 ~35 秒，四种组合都绕不过。
算法表是进程级缓存的，同进程内第二次建 session 只要 1 秒 —— 所以唯一的解法
是常驻服务，而不是调这些参数。

用法：
  .\\.venv\\Scripts\\python.exe experiments\\bench_cudnn.py [--image path]
  不给 --image 就用合成噪点图，只测 cuDNN 算法搜索的耗时。
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import cuda_dlls

cuda_dlls.register()

import cv2
import numpy as np
import onnxruntime as ort

from ui_locate.detect import letterbox, to_tensor
from ui_locate.detect import default_model


def load_input(path: str | None) -> np.ndarray:
    if path:
        img = cv2.imread(path)
        if img is None:
            raise SystemExit(f"读不到图片: {path}")
    else:
        # 合成图：测 cuDNN 算法搜索耗时不需要真实界面
        rng = np.random.default_rng(0)
        img = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)
    c, _s, _dx, _dy = letterbox(img)
    return to_tensor(c)


COMBOS = [
    ("EXHAUSTIVE", "1"),
    ("EXHAUSTIVE", "0"),
    ("HEURISTIC", "1"),
    ("HEURISTIC", "0"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=None)
    args = ap.parse_args()
    x = load_input(args.image)

    print(f"{'search':12s} {'max_ws':6s} {'ctor':>9s} {'first':>9s} {'steady':>8s}")
    for search, ws in COMBOS:
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.log_severity_level = 3
        t0 = time.perf_counter()
        sess = ort.InferenceSession(
            str(default_model()),
            so,
            providers=[
                (
                    "CUDAExecutionProvider",
                    {"cudnn_conv_algo_search": search, "cudnn_conv_use_max_workspace": ws},
                ),
                "CPUExecutionProvider",
            ],
        )
        t1 = time.perf_counter()
        sess.run(None, {sess.get_inputs()[0].name: x})
        t2 = time.perf_counter()
        for _ in range(20):
            sess.run(None, {sess.get_inputs()[0].name: x})
        t3 = time.perf_counter()
        print(
            f"{search:12s} {ws:6s} {(t1-t0)*1000:8.0f}ms {(t2-t1)*1000:8.0f}ms "
            f"{(t3-t2)/20*1000:7.2f}ms",
            flush=True,
        )


if __name__ == "__main__":
    main()
