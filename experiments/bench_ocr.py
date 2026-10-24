"""拆解 RapidOCR 的耗时构成，找出可调参数。

结论（已实测，别再重复试）：默认参数已是最优平衡。一张 1280×720 的界面截图上
baseline 436ms / use_cls=False 425ms / max_side_len=960 354ms（但掉框）/
640 319ms（掉更多）/ threads=8 440ms / text_score=0.8 442ms。
OCR 必须走 CPU —— CUDA 上小图搬运开销反而让整页 OCR 慢 5 倍。

用法：
  .\\.venv\\Scripts\\python.exe experiments\\bench_ocr.py [--image path]
  不给 --image 就用合成图（画几行字），只测 OCR 各组参数耗时。
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


def load_input(path: str | None) -> np.ndarray:
    if path:
        img = cv2.imread(path)
        if img is None:
            raise SystemExit(f"读不到图片: {path}")
        return img
    # 合成一张带中文/英文/数字的白底图，够 RapidOCR 检出几行框
    img = np.full((720, 1280, 3), 245, dtype=np.uint8)
    rows = [
        "File  Edit  View  Help",
        "Ctrl+S  Enter  Esc  Shift",
        "00:00   163ms   item 1",
        "Settings  Plugins  Automations",
    ]
    for i, txt in enumerate(rows):
        cv2.putText(
            img, txt, (60, 140 + i * 150),
            cv2.FONT_HERSHEY_SIMPLEX, 1.6, (30, 30, 30), 3, cv2.LINE_AA,
        )
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=None)
    args = ap.parse_args()
    img = load_input(args.image)
    print(f"image: {img.shape[1]}x{img.shape[0]}")

    from rapidocr import RapidOCR

    def trial(tag: str, params: dict) -> None:
        p = {"Global.log_level": "error"}
        p.update(params)
        ocr = RapidOCR(params=p)
        ocr(img)  # warmup
        n = 3
        t = time.perf_counter()
        r = None
        for _ in range(n):
            r = ocr(img)
        ms = (time.perf_counter() - t) / n * 1000
        cnt = 0 if (r is None or r.boxes is None) else len(r.boxes)
        print(f"{tag:36s} {ms:7.1f} ms  boxes={cnt}", flush=True)

    trial("baseline (det+cls+rec)", {})
    trial("no cls", {"Global.use_cls": False})
    trial("max_side_len=960", {"Global.max_side_len": 960})
    trial("max_side_len=640", {"Global.max_side_len": 640})
    trial("no preprocess", {"Global.use_preprocess_img": False})
    trial("no vertical_padding", {"Global.use_vertical_padding": False})
    trial("threads=8", {"EngineConfig.onnxruntime.intra_op_num_threads": 8})
    trial("text_score=0.8", {"Global.text_score": 0.8})


if __name__ == "__main__":
    main()
