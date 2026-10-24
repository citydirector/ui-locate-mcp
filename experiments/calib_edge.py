"""校准「跳过空白块」的边缘密度阈值。

MIN_EDGE_RATIO 定得不对有两种坏处：
  - 太高 → 把稀疏但有用的块跳掉，漏检小按钮；
  - 太低 → 空白块照样送进模型，白算。

这里把真实屏幕切块后，逐块报边缘密度，看空白块和有效块实际差多少。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import screen  # noqa: E402
from ui_locate.tiled import _edge_map, _ink_profile, plan_tiles  # noqa: E402


def report(name: str, bgr: np.ndarray, max_side: int = 960) -> None:
    h, w = bgr.shape[:2]
    print(f"\n=== {name}  {w}x{h} ===")

    plan = plan_tiles(w, h, max_side, 0.15, bgr=bgr, min_edge_ratio=0.0)
    print(f"切块 {len(plan.tiles)} 块（min_edge_ratio=0 时全部保留）")

    edges = _edge_map(bgr)
    rows = []
    for i, (x0, y0, x1, y1) in enumerate(plan.tiles):
        seg = edges[y0:y1, x0:x1]
        ratio = float((seg > 0).mean()) if seg.size else 0.0
        # 这块里有多少"非背景像素"，作为第二判据
        colf, rowf = _ink_profile(bgr[y0:y1, x0:x1])
        ink = float(max(colf.mean(), rowf.mean()))
        rows.append((i, x0, y0, x1 - x0, y1 - y0, ratio, ink))

    rows.sort(key=lambda r: r[5])
    print(f"{'#':>3} {'x,y':>12} {'尺寸':>11} {'edge':>9} {'ink':>8}")
    for (i, x0, y0, tw, th, ratio, ink) in rows:
        print(f"{i:>3} {x0:>5},{y0:>5} {tw:>5}x{th:<5} {ratio:>9.5f} {ink:>8.4f}")

    vals = np.array([r[5] for r in rows])
    print(
        f"edge 分布: min={vals.min():.5f} p25={np.percentile(vals, 25):.5f} "
        f"中位={np.median(vals):.5f} max={vals.max():.5f}"
    )


def main() -> None:
    screen.set_dpi_aware()

    for mon in ("0", "1", None):
        label = {None: "整个虚拟桌面"}.get(mon, f"monitor {mon}")
        try:
            bgr, origin = screen.grab(mon)
        except Exception as e:
            print(f"{label}: 抓屏失败 {type(e).__name__}: {e}")
            continue
        report(f"{label} origin={origin}", bgr)


if __name__ == "__main__":
    main()
