"""一行命令完成：截图 → 检测元素 → OCR 打标签 → 输出可查询的元素表。

这是替代"反复截图盲猜坐标"的最终入口。

用法：
  # 从当前屏幕抓一张，列出所有可点元素
  uv run python -m ui_locate.locate

  # 按名字直接问坐标
  uv run python -m ui_locate.locate --find 保存 --find 取消

  # 指定截图文件、调阈值、导出
  uv run python -m ui_locate.locate --image shot.png --conf 0.25 --json out.json

输出：每个元素一行，含编号、中心坐标、尺寸、置信度、文字。
      --json 导出完整数据，供后续精确取坐标。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from ui_locate import cuda_dlls

cuda_dlls.register()

from ui_locate import screen  # noqa: E402
from ui_locate.annotate import build_sheet  # noqa: E402
from ui_locate import config  # noqa: E402
from ui_locate.detect import Detector, default_model, visualize  # noqa: E402
from ui_locate.ocr_match import OcrLine, attach_text, find  # noqa: E402

# CLI 的临时截图落在工程自己的 .cache 下，不往别的工作区写东西
DEFAULT_OUT = Path(__file__).resolve().parents[2] / ".cache" / "out"


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，中文标签会变成乱码。强制 stdout 走 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass


def grab_screen(monitor: object = None) -> np.ndarray:
    """抓一块屏（默认整块虚拟桌面），返回 BGR。

    兼容旧签名：以前这个函数只抓主屏、没有参数。现在委托给
    `ui_locate.screen.grab`，多显示器和负坐标坐标系都在那一层处理。

    只想要坐标时用 `grab_screen_with_origin` —— 图内坐标必须加上 origin
    才是能交给 cua_driver 的屏幕绝对坐标。
    """
    return screen.grab(monitor)[0]


def grab_screen_with_origin(monitor: object = None) -> tuple[np.ndarray, tuple[int, int]]:
    """抓屏并返回左上角的屏幕绝对坐标，供坐标换算用。"""
    return screen.grab(monitor)


class Ocr:
    """整页 OCR 封装。首次调用加载模型。

    cuda 默认关：实测 PP-OCR 模型太小，CUDA 上每次调用要在 CPU↔GPU 之间
    搬运大量小图裁片，反而比 CPU 慢 5 倍（2135ms vs 442ms）。大模型走 GPU，
    小模型走 CPU —— 这是这类"检测大 + 识别小"流水线的固定搭配。
    """

    def __init__(self, cuda: bool = False) -> None:
        from rapidocr import RapidOCR

        params = {"Global.log_level": "error"}
        if cuda:
            params["EngineConfig.onnxruntime.use_cuda"] = True
            params[
                "EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search"
            ] = "HEURISTIC"
        self.engine = RapidOCR(params=params)

    def lines(self, bgr: np.ndarray) -> list[OcrLine]:
        res = self.engine(bgr)
        if res.boxes is None or res.txts is None:
            return []
        out: list[OcrLine] = []
        for box, txt, score in zip(res.boxes, res.txts, res.scores):
            pts = np.asarray(box, dtype=np.float32)
            out.append(
                OcrLine(
                    text=str(txt),
                    score=float(score),
                    x1=float(pts[:, 0].min()),
                    y1=float(pts[:, 1].min()),
                    x2=float(pts[:, 0].max()),
                    y2=float(pts[:, 1].max()),
                )
            )
        return out


def main() -> None:
    _force_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=Path, default=None, help="不给则抓当前屏幕")
    ap.add_argument("--model", type=Path, default=None, help="模型文件；缺省按 ui_locate.config 规则查找")
    ap.add_argument(
        "--provider", choices=["auto", "cuda", "cpu"], default=config.provider()
    )
    ap.add_argument("--conf", type=float, default=0.3)
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--tiled", action="store_true")
    ap.add_argument("--max-side", type=int, default=None, help="每块长边；缺省读 UI_LOCATE_MAX_SIDE 或 960")
    ap.add_argument("--overlap", type=float, default=0.15)
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--find", action="append", default=[], metavar="TEXT")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--vis", type=Path, default=None)
    ap.add_argument("--sheet", type=Path, default=None)
    args = ap.parse_args()

    t_start = time.perf_counter()

    if args.image:
        bgr = cv2.imread(str(args.image), cv2.IMREAD_COLOR)
        if bgr is None:
            raise SystemExit(f"无法读取图片: {args.image}")
    else:
        bgr = grab_screen()
    t_grab = (time.perf_counter() - t_start) * 1000

    if args.max_side is None:
        args.max_side = config.max_side()

    det = Detector(args.model, config.resolve_provider(args.provider))

    def run() -> tuple[list[dict], float]:
        t0 = time.perf_counter()
        if args.tiled:
            from ui_locate.tiled import detect_tiled

            items, _ = detect_tiled(
                det.sess, det.iname, det.onames, bgr,
                args.conf, args.iou,
                max_side=args.max_side, overlap=args.overlap,
            )
        else:
            items, _ = det.detect(bgr, args.conf, args.iou)
        return items, (time.perf_counter() - t0) * 1000

    run()  # 预热

    det_ms_sum = 0.0
    N = 3
    for _ in range(N):
        items, det_ms = run()
        det_ms_sum += det_ms
    det_ms = det_ms_sum / N

    ocr_ms = 0.0
    if not args.no_ocr:
        ocr = Ocr(cuda=config.ocr_on_cuda())
        ocr.lines(bgr)  # 预热
        t0 = time.perf_counter()
        lines = ocr.lines(bgr)
        ocr_ms = (time.perf_counter() - t0) * 1000
        attach_text(items, lines)

    total = det_ms + ocr_ms

    print(f"image    : {bgr.shape[1]}x{bgr.shape[0]}"
          + (f"  ({args.image.name})" if args.image else "  (live screen)"))
    print(f"grab     : {t_grab:.1f} ms")
    print(f"detect   : {det_ms:.1f} ms  ({'tiled' if args.tiled else 'single'}"
          f", conf>={args.conf})")
    if not args.no_ocr:
        print(f"ocr      : {ocr_ms:.1f} ms")
    print(f"total    : {total:.1f} ms")
    print(f"elements : {len(items)}\n")

    # 有文字的元素优先展示，视觉模型读起来更有用
    labeled = [it for it in items if it.get("text")]
    unlabeled = [it for it in items if not it.get("text")]
    ordered = labeled + unlabeled

    for i, it in enumerate(ordered):
        tag = f"  {it['text']!r}" if it.get("text") else ""
        flag = " T" if it.get("text_only") else "  "
        print(
            f"  [{i:2d}]{flag}({it['cx']:7.1f}, {it['cy']:7.1f})  "
            f"{it['w']:5.1f}x{it['h']:5.1f}  conf={it['conf']:.2f}{tag}"
        )

    if args.find:
        print()
        for q in args.find:
            hits = find(ordered, q)
            if not hits:
                print(f"find {q!r}: 没找到")
                continue
            for h in hits:
                print(
                    f"find {q!r} -> ({h['cx']:.1f}, {h['cy']:.1f})"
                    f"  {h['w']:.0f}x{h['h']:.0f}  {h.get('text')!r}"
                )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "size": [bgr.shape[1], bgr.shape[0]],
                    "provider": det.active,
                    "tiled": args.tiled,
                    "conf": args.conf,
                    "detect_ms": round(det_ms, 2),
                    "ocr_ms": round(ocr_ms, 2),
                    "total_ms": round(total, 2),
                    "elements": ordered,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\njson  -> {args.json}")
    if args.vis:
        args.vis.parent.mkdir(parents=True, exist_ok=True)
        visualize(bgr, ordered, args.vis)
        print(f"vis   -> {args.vis}")
    if args.sheet:
        args.sheet.parent.mkdir(parents=True, exist_ok=True)
        for p in build_sheet(bgr, ordered, args.sheet):
            print(f"sheet -> {p}")


if __name__ == "__main__":
    main()
