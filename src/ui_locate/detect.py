"""OmniParser icon_detect 端到端 UI 元素检测。

输入一张截图，输出可点击元素的屏幕坐标列表。替代"看缩略图盲猜坐标"。

用法：
  uv run python -m ui_locate.detect --image shot.png
  uv run python -m ui_locate.detect --image shot.png --conf 0.25 --json out.json
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

from ui_locate import config, cuda_dlls

cuda_dlls.register()

import onnxruntime as ort  # noqa: E402

INPUT_SIZE = 640
PAD_VALUE = 114


def default_model() -> Path:
    """默认模型路径，按 ``ui_locate.config`` 的规则推导。"""
    return config.model_path()


def letterbox(img: np.ndarray, size: int = INPUT_SIZE) -> tuple[np.ndarray, float, int, int]:
    """等比缩放并居中填充到 size×size，返回 (画布, 缩放比, x偏移, y偏移)。"""
    h, w = img.shape[:2]
    scale = min(size / w, size / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), PAD_VALUE, dtype=np.uint8)
    dx, dy = (size - nw) // 2, (size - nh) // 2
    canvas[dy : dy + nh, dx : dx + nw] = resized
    return canvas, scale, dx, dy


def to_tensor(bgr: np.ndarray) -> np.ndarray:
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return np.ascontiguousarray(rgb.transpose(2, 0, 1)[None, ...])


def nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / np.maximum(1e-9, areas[i] + areas[order[1:]] - inter)
        order = order[1:][iou <= iou_thr]
    return keep


def decode(
    out: np.ndarray, scale: float, dx: int, dy: int, conf: float, iou: float
) -> list[dict]:
    """把 [1,5,8400] 的原始输出解成原图坐标下的框。

    输出布局是 cx, cy, w, h, score —— 单类检测器，没有类别维度。
    """
    pred = out[0].T  # [8400, 5]
    scores = pred[:, 4]
    mask = scores >= conf
    pred, scores = pred[mask], scores[mask]
    if pred.shape[0] == 0:
        return []

    cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
    boxes = np.stack(
        [cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1
    ).astype(np.float32)

    keep = nms(boxes, scores, iou)
    boxes, scores = boxes[keep], scores[keep]

    # 反 letterbox：先减 padding，再除缩放比
    boxes[:, [0, 2]] = (boxes[:, [0, 2]] - dx) / scale
    boxes[:, [1, 3]] = (boxes[:, [1, 3]] - dy) / scale
    boxes = np.clip(boxes, 0, None)

    items = []
    for (x1, y1, x2, y2), s in zip(boxes, scores):
        items.append(
            {
                "x1": round(float(x1), 1),
                "y1": round(float(y1), 1),
                "x2": round(float(x2), 1),
                "y2": round(float(y2), 1),
                "cx": round(float((x1 + x2) / 2), 1),
                "cy": round(float((y1 + y2) / 2), 1),
                "w": round(float(x2 - x1), 1),
                "h": round(float(y2 - y1), 1),
                "conf": round(float(s), 3),
            }
        )
    items.sort(key=lambda d: d["conf"], reverse=True)
    return items


class Detector:
    """常驻检测器：一次加载模型，反复检测。"""

    # cuDNN 调优参数（实测数据，见 scripts/bench_cudnn.py）：
    #   EXHAUSTIVE + max_workspace=1 → 首次 34306ms（8GB 显存放不下 workspace 表，反复重算）
    #   EXHAUSTIVE + max_workspace=0 → 首次  1035ms，稳态 10.80ms  ← 采用
    #   HEURISTIC  + max_workspace=1 → 首次  1014ms，稳态 11.71ms
    # 根因：max_workspace=1 时 cuDNN 尝试为每层保留最大工作区，8GB 卡上
    # 大量层回退到 Fallback 模式（"May be extremely slow"），首帧反复试算。
    CUDA_OPTS = {
        "cudnn_conv_algo_search": "EXHAUSTIVE",
        "cudnn_conv_use_max_workspace": "0",
        "do_copy_in_default_stream": "1",
    }

    def __init__(self, model: Path | None = None, provider: str = "auto") -> None:
        model = Path(model) if model is not None else default_model()
        provider = config.resolve_provider(provider)
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.log_severity_level = 3
        if provider == "cuda":
            providers: list = [("CUDAExecutionProvider", dict(self.CUDA_OPTS))]
            providers.append("CPUExecutionProvider")
        else:
            providers = ["CPUExecutionProvider"]
        self.sess = ort.InferenceSession(str(model), so, providers=providers)
        self.active = self.sess.get_providers()[0]
        self.iname = self.sess.get_inputs()[0].name
        self.onames = [o.name for o in self.sess.get_outputs()]

    def detect(
        self, bgr: np.ndarray, conf: float = 0.3, iou: float = 0.5
    ) -> tuple[list[dict], float]:
        """返回 (元素列表, 耗时ms)。"""
        t0 = time.perf_counter()
        canvas, scale, dx, dy = letterbox(bgr)
        x = to_tensor(canvas)
        out = self.sess.run(self.onames, {self.iname: x})[0]
        items = decode(out, scale, dx, dy, conf=conf, iou=iou)
        return items, (time.perf_counter() - t0) * 1000


def visualize(bgr: np.ndarray, items: list[dict], dst: Path) -> None:
    vis = bgr.copy()
    for i, it in enumerate(items):
        cv2.rectangle(
            vis,
            (int(it["x1"]), int(it["y1"])),
            (int(it["x2"]), int(it["y2"])),
            (0, 255, 0),
            2,
        )
        cv2.putText(
            vis,
            f"{i}",
            (int(it["x1"]), max(12, int(it["y1"]) - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 255, 255),
            1,
            cv2.LINE_AA,
        )
    cv2.imwrite(str(dst), vis)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=Path, required=True)
    ap.add_argument("--model", type=Path, default=None, help="模型文件；缺省按 ui_locate.config 规则查找")
    ap.add_argument("--provider", choices=["auto", "cuda", "cpu"], default=config.provider())
    ap.add_argument("--conf", type=float, default=0.3)
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--tiled", action="store_true", help="大图分块检测，找回小元素")
    ap.add_argument("--max-side", type=int, default=INPUT_SIZE)
    ap.add_argument("--overlap", type=float, default=0.15)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--vis", type=Path, default=None)
    args = ap.parse_args()

    bgr = cv2.imread(str(args.image), cv2.IMREAD_COLOR)
    if bgr is None:
        raise SystemExit(f"无法读取图片: {args.image}")

    det = Detector(args.model, args.provider)

    if args.tiled:
        from ui_locate.tiled import detect_tiled

        # 预热
        detect_tiled(
            det.sess, det.iname, det.onames, bgr, args.conf, args.iou,
            max_side=args.max_side, overlap=args.overlap,
        )
        runs = 3
        t0 = time.perf_counter()
        for _ in range(runs):
            items, stats = detect_tiled(
                det.sess, det.iname, det.onames, bgr, args.conf, args.iou,
                max_side=args.max_side, overlap=args.overlap,
            )
        avg = (time.perf_counter() - t0) / runs * 1000
        print(
            f"tiled    : {stats['tiles']} tiles  "
            f"raw={stats['tile_boxes']} -> kept={stats['kept']}"
        )
    else:
        det.detect(bgr, args.conf, args.iou)
        runs = 5
        t0 = time.perf_counter()
        for _ in range(runs):
            items, _ = det.detect(bgr, args.conf, args.iou)
        avg = (time.perf_counter() - t0) / runs * 1000

    print(f"image    : {args.image.name}  {bgr.shape[1]}x{bgr.shape[0]}")
    print(f"provider : {det.active}")
    print(f"model    : {args.model.name} ({args.model.stat().st_size / 1e6:.1f} MB)")
    print(f"threshold: conf>={args.conf}  iou<={args.iou}")
    print(f"latency  : {avg:.2f} ms  ({1000 / avg:.1f} FPS)")
    print(f"elements : {len(items)}\n")
    for i, it in enumerate(items[:40]):
        print(
            f"  [{i:2d}] ({it['cx']:7.1f}, {it['cy']:7.1f})  "
            f"{it['w']:5.1f}x{it['h']:5.1f}  conf={it['conf']:.3f}"
        )
    if len(items) > 40:
        print(f"  ... {len(items) - 40} more")

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "image": str(args.image),
                    "size": [bgr.shape[1], bgr.shape[0]],
                    "provider": det.active,
                    "avg_ms": round(avg, 2),
                    "elements": items,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nJSON -> {args.json}")
    if args.vis:
        visualize(bgr, items, args.vis)
        print(f"VIS  -> {args.vis}")


if __name__ == "__main__":
    main()
