"""把大截图切块送给 YOLO，找回被缩放吃掉的小元素。

**为什么必须切块**

一张高分屏桌面压到 640x640，可能缩小四五倍，40px 的图标只剩几个像素。
检测器能框住大按钮，但小图标要么漏检、要么框不准。切块后每块单独缩放到
640，等效放大，小元素就回得来了。屏幕越大这个差距越明显。

**为什么不能只做均匀网格（这是本次改动的重点）**

均匀切块有两个毛病：

  1. **切在元素中间**。网格线是算出来的，不认内容，正好穿过一个按钮就把
     它劈成两半，两边各剩半截，检测器谁都不敢框。
  2. **空白也照样扫**。实测一个高分屏桌面，61% 是非黑像素，但真正
     有内容的区域远小于此 —— 大面积纯色背景扫一遍纯属浪费。

所以这里做的是「内容感知切块」：

  - 先按 `max_side` 算出理想网格；
  - 每条网格线在 ±overlap 的窗口内微调，挪到附近**最空**的那一列/行
    （用逐列/逐行的 ink 投影找最小值），让切缝落在元素之间的空隙里；
  - 切完检查每一块的**边缘密度**，几乎没边缘的块直接跳过（没有文字、
    没有图标的地方，检测器本来就找不出东西）；
  - 跨块重复检出的同一个元素用全局 NMS 合并。

跳过的块不进模型，所以省下来的是实打实的推理时间。

**坐标系**

本模块只处理「图内坐标」，不认识屏幕。图内坐标 → 屏幕绝对坐标的换算由
`ui_locate.screen` 的 origin 负责，别在这里混进来。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from ui_locate import config
from ui_locate.detect import INPUT_SIZE, decode, letterbox, nms, to_tensor

# 判断"这一块值得扫"的边缘密度下限。取值来自 ``ui_locate.config``，
# 可用环境变量 UI_LOCATE_MIN_EDGE 覆盖；这里保留模块级别名只为兼容旧调用。
MIN_EDGE_RATIO = config.min_edge_ratio()


@dataclass
class TilePlan:
    """切块方案。tiles 里每项是图内坐标下的 (x0, y0, x1, y1)。"""

    tiles: list[tuple[int, int, int, int]] = field(default_factory=list)
    overlap: float = 0.15
    # 被判定为空白、不送进模型的块数
    skipped: int = 0
    # "grid" = 纯几何切分，"content" = 内容感知（对齐切缝 + 跳空白）
    source: str = "grid"

    def as_dict(self) -> dict:
        return {
            "tiles": len(self.tiles),
            "skipped": self.skipped,
            "source": self.source,
            "overlap": self.overlap,
        }


def _edge_map(bgr: np.ndarray) -> np.ndarray:
    """二值边缘图。文字和图标一定产生边缘，纯色区域没有。"""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    return cv2.Canny(gray, 60, 160)


def _ink_profile(bgr: np.ndarray, tol: int = 12) -> tuple[np.ndarray, np.ndarray]:
    """逐列 / 逐行的「非背景像素占比」。

    背景色用整图中位数估计：深色主题下是中位深色、浅色主题下是中位浅色，
    两种情况「与背景差异大的像素」都是内容。返回 (col_frac, row_frac)，
    元素已归一化到 0~1，与图像尺寸无关。
    """
    bg = np.median(bgr.reshape(-1, 3), axis=0)
    diff = np.abs(bgr.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    ink = (diff > tol).astype(np.float32)
    h, w = ink.shape
    return ink.mean(axis=0), ink.mean(axis=1)


def _snap(pos: int, profile: np.ndarray, window: int, lo: int, hi: int) -> int:
    """把切缝挪到 ±window 内最空的位置。

    只在 [lo, hi] 里找，保证不会把块挪到越界或者把块撑得过大。
    """
    a = max(lo, pos - window)
    b = min(hi, pos + window)
    if b <= a:
        return min(max(pos, lo), hi)
    seg = profile[a:b]
    return a + int(np.argmin(seg))


def _uniform_axis(total: int, max_side: int, overlap: float) -> tuple[list[int], int]:
    """沿一个方向算出切缝位置列表（含首尾），以及重叠量。

    每块目标边长取 max_side 的 0.9，留出余量给重叠，避免切完超尺寸。
    """
    target = max(1, int(max_side * 0.9))
    n = max(1, -(-total // target))
    step = -(-total // n)  # 向上取整，保证 n 段能盖满
    bounds = [min(total, i * step) for i in range(n)] + [total]
    return bounds, int(step * overlap)


def plan_tiles(
    w: int,
    h: int,
    max_side: int = INPUT_SIZE,
    overlap: float = 0.15,
    bgr: np.ndarray | None = None,
    min_edge_ratio: float = MIN_EDGE_RATIO,
) -> TilePlan:
    """算出切块方案。

    参数：
      w, h            图尺寸
      max_side        每块长边上限（软目标，实际可略超）
      overlap         相邻块的重叠比例
      bgr             给了图就做内容感知（对齐切缝 + 跳空白）；不给则纯几何切分
      min_edge_ratio  边缘密度低于此值的块跳过

    内容感知只是**改良**，不是必需：`bgr=None` 时行为与旧版一致，
    方便在不方便读图的地方（比如只想估块数）继续用。
    """
    if max_side <= 0:
        raise ValueError("max_side 必须为正")

    if max(w, h) <= max_side and bgr is None:
        return TilePlan([(0, 0, w, h)], overlap, 0, "grid")

    xb, ox = _uniform_axis(w, max_side, overlap)
    yb, oy = _uniform_axis(h, max_side, overlap)

    source = "grid"
    edges: np.ndarray | None = None

    if bgr is not None:
        col_frac, row_frac = _ink_profile(bgr)
        # 切缝微调：内部的每条边界都往最空的列/行挪
        for i in range(1, len(xb) - 1):
            xb[i] = _snap(xb[i], col_frac, ox, xb[i - 1] + 1, xb[i + 1] - 1)
        for i in range(1, len(yb) - 1):
            yb[i] = _snap(yb[i], row_frac, oy, yb[i - 1] + 1, yb[i + 1] - 1)
        source = "content"
        edges = _edge_map(bgr)

    tiles: list[tuple[int, int, int, int]] = []
    skipped = 0
    for r, c in itertools.product(range(len(yb) - 1), range(len(xb) - 1)):
        x0 = max(0, xb[c] - ox)
        y0 = max(0, yb[r] - oy)
        x1 = min(w, xb[c + 1] + ox)
        y1 = min(h, yb[r + 1] + oy)
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        if edges is not None:
            seg = edges[y0:y1, x0:x1]
            ratio = float((seg > 0).mean()) if seg.size else 0.0
            if ratio < min_edge_ratio:
                skipped += 1
                continue
        tiles.append((x0, y0, x1, y1))

    if not tiles:
        # 全被跳过说明整图几乎没内容，至少给它一整块，别返回空
        return TilePlan([(0, 0, w, h)], overlap, skipped, source)

    return TilePlan(tiles, overlap, skipped, source)


def detect_tiled(
    sess,
    iname: str,
    onames: list[str],
    bgr: np.ndarray,
    conf: float = 0.3,
    iou: float = 0.5,
    max_side: int = INPUT_SIZE,
    overlap: float = 0.15,
    smart: bool = True,
    min_edge_ratio: float = MIN_EDGE_RATIO,
) -> tuple[list[dict], dict]:
    """分块检测 + 全局 NMS。返回 (元素列表, 统计信息)。

    smart=True 时走内容感知切块（对齐切缝、跳过空白块），False 则纯几何网格。
    """
    h, w = bgr.shape[:2]
    plan = plan_tiles(
        w, h, max_side, overlap,
        bgr=bgr if smart else None,
        min_edge_ratio=min_edge_ratio,
    )

    t0 = time.perf_counter()
    boxes: list[list[float]] = []
    scores: list[float] = []
    for (x0, y0, x1, y1) in plan.tiles:
        crop = bgr[y0:y1, x0:x1]
        canvas, scale, dx, dy = letterbox(crop)
        x = to_tensor(canvas)
        out = sess.run(onames, {iname: x})[0]
        for it in decode(out, scale, dx, dy, conf=conf, iou=iou):
            boxes.append([it["x1"] + x0, it["y1"] + y0, it["x2"] + x0, it["y2"] + y0])
            scores.append(it["conf"])
    elapsed = (time.perf_counter() - t0) * 1000

    items: list[dict] = []
    if boxes:
        b = np.asarray(boxes, dtype=np.float32)
        s = np.asarray(scores, dtype=np.float32)
        keep = nms(b, s, iou)
        for i in keep:
            x1, y1, x2, y2 = b[i]
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
                    "conf": round(float(s[i]), 3),
                }
            )
        items.sort(key=lambda d: d["conf"], reverse=True)

    stats = {
        "tiles": len(plan.tiles),
        "skipped": plan.skipped,
        "source": plan.source,
        "tile_boxes": len(boxes),
        "kept": len(items),
        "ms": round(elapsed, 2),
        "max_side": max_side,
        "overlap": overlap,
    }
    return items, stats
