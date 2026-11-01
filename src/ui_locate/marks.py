"""编号标注（Set-of-Mark）：把检测结果画成「带编号的截图」，让模型选号而不是报坐标。

**为什么**

视觉模型普遍「认得出」却「报不准」：它能看出哪个是保存按钮，但让它直接给
像素坐标，误差常常几十像素 —— 在专业软件那种 16px 的小按钮上就是点错。
把问题换成选择题以后，坐标由检测器算、模型只负责认：

    模型看到：一张截图，每个可点的东西上都有个编号
    模型回答：「#17」
    插件执行：#17 的中心坐标（检测器给的，像素级）

**本模块只做纯计算**（不抓屏、不碰 ONNX），所以能在任何平台上单测：

  - ``build_marks``  检测结果 → 去重、排序、编号后的 ``Mark`` 列表
  - ``render``       截图 + Mark → 标注图（JPEG / PNG 字节）
  - ``MarkStore``    记住最近一次的编号集合，供 act 把 ``#17`` 解回坐标

**坐标约定** 与其它模块一致：Mark 里的坐标全是屏幕绝对坐标；标注图另有自己的
像素坐标系，``scale`` 与 ``origin`` 说明怎么换算，但调用方一般用不上 ——
直接报编号即可。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

# 高对比、彼此可分的一组颜色（BGR）。相邻编号颜色不同，挤在一起的框也分得清。
PALETTE: tuple[tuple[int, int, int], ...] = (
    (40, 40, 230),    # 红
    (230, 120, 20),   # 蓝
    (30, 170, 30),    # 绿
    (200, 40, 200),   # 品红
    (0, 140, 255),    # 橙
    (180, 160, 0),    # 青
    (60, 60, 60),     # 深灰
    (120, 0, 160),    # 紫红
    (0, 200, 200),    # 黄
    (160, 80, 0),     # 深蓝
)

FONT = cv2.FONT_HERSHEY_SIMPLEX


@dataclass
class Mark:
    id: int
    x1: float
    y1: float
    x2: float
    y2: float
    text: str = ""
    conf: float = 0.0
    kind: str = "icon"  # icon（检测框）/ text（认出了字）/ ocr（只有文字）/ cell（成排成列的格子）

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def h(self) -> float:
        return self.y2 - self.y1

    def brief(self) -> dict:
        """给模型看的精简版：编号、文字、中心、尺寸。"""
        out = {
            "id": self.id,
            "cx": int(round(self.cx)),
            "cy": int(round(self.cy)),
            "w": int(round(self.w)),
            "h": int(round(self.h)),
        }
        if self.text:
            out["text"] = self.text
        if self.kind != "icon":
            out["kind"] = self.kind
        return out

    def as_target(self) -> dict:
        """act 能直接吃的目标引用（``at`` 写法）。"""
        return {
            "text": self.text or f"#{self.id}",
            "cx": self.cx,
            "cy": self.cy,
            "w": self.w,
            "h": self.h,
            "conf": self.conf or 1.0,
        }


def _iou_and_cover(a: tuple, b: tuple) -> tuple[float, float]:
    """返回 (IoU, 较小者被覆盖的比例)。"""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0, 0.0
    area_a = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / (area_a + area_b - inter), inter / min(area_a, area_b)


BLANK_EDGE_DENSITY = 0.003


def drop_blank(elements: list[dict], bgr: np.ndarray, origin: tuple[int, int],
               stats: dict | None = None) -> list[dict]:
    """去掉「空白区凑出来的框」：框里（含边）几乎没有边缘、也没有文字。

    纯色 / 渐变背景上的误检会让标注数虚高（regress3：590×70、65×70 两个空框）。
    有边框的空物品格、空槽位边上有一圈边缘，密度远高于阈值，不受影响。
    """
    import cv2

    ox, oy = origin
    H, W = bgr.shape[:2]
    keep: list[dict] = []
    dropped = 0
    for it in elements:
        if (it.get("text") or "").strip():
            keep.append(it)
            continue
        try:
            x1 = max(0, int(round(float(it["x1"]) - ox)) - 1)
            y1 = max(0, int(round(float(it["y1"]) - oy)) - 1)
            x2 = min(W, int(round(float(it["x2"]) - ox)) + 1)
            y2 = min(H, int(round(float(it["y2"]) - oy)) + 1)
        except (KeyError, TypeError, ValueError):
            keep.append(it)
            continue
        if x2 - x1 < 6 or y2 - y1 < 6:
            keep.append(it)
            continue
        crop = bgr[y1:y2, x1:x2]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        density = float(np.count_nonzero(cv2.Canny(gray, 50, 150))) / gray.size
        if density < BLANK_EDGE_DENSITY:
            dropped += 1
            continue
        keep.append(it)
    if stats is not None:
        stats["blank_dropped"] = stats.get("blank_dropped", 0) + dropped
    return keep


def build_marks(
    elements: list[dict],
    *,
    frame_area: float | None = None,
    max_marks: int = 200,
    iou_dup: float = 0.7,
    max_area_ratio: float = 0.35,
    min_side: float = 3.0,
    stats: dict | None = None,
) -> list[Mark]:
    """检测结果 → 编号好的 Mark 列表。

    规则：
      1. 丢掉退化框（边长 < ``min_side``）和超大框（面积超过整帧
         ``max_area_ratio``，那是面板 / 窗口，不是可点的东西，画上去只添乱）。
      2. 去重：两个框 IoU ≥ ``iou_dup``，或者大小相近（面积比 ≥ 0.6）且小的被
         覆盖 90% 以上，只留一个 —— 带文字的优先，再看置信度。
         大框套小框（工具栏里的按钮）**不算重复**，两个都留。
      3. 超过 ``max_marks`` 时按「带文字 > 置信度」截断。
      4. 按阅读顺序编号：先按行（行高取框高中位数），行内从左到右。
         编号顺序和画面位置一致，模型更容易在图里找到自己要的号。
    """
    cands: list[dict] = []
    for it in elements:
        try:
            x1, y1, x2, y2 = (float(it[k]) for k in ("x1", "y1", "x2", "y2"))
        except (KeyError, TypeError, ValueError):
            continue
        if x2 - x1 < min_side or y2 - y1 < min_side:
            continue
        if frame_area and (x2 - x1) * (y2 - y1) > max_area_ratio * frame_area:
            continue
        text = str(it.get("text") or "").strip()
        conf = float(it.get("conf") or 0.0)
        if it.get("text_only"):
            kind = "ocr"
        elif text:
            kind = "text"
        elif it.get("source") == "grid":
            kind = "cell"
        else:
            kind = "icon"
        cands.append({"box": (x1, y1, x2, y2), "text": text, "conf": conf, "kind": kind})

    # 优先级：带文字 > 置信度。去重和截断都按这个顺序保留。
    cands.sort(key=lambda c: (bool(c["text"]), c["conf"]), reverse=True)
    kept: list[dict] = []
    for c in cands:
        a = c["box"]
        area_a = (a[2] - a[0]) * (a[3] - a[1])
        dup = False
        for k in kept:
            b = k["box"]
            iou, cover = _iou_and_cover(a, b)
            if iou >= iou_dup:
                dup = True
                break
            area_b = (b[2] - b[0]) * (b[3] - b[1])
            similar = min(area_a, area_b) / max(area_a, area_b) >= 0.6
            if similar and cover >= 0.9:
                dup = True
                break
        if not dup:
            kept.append(c)

    # 密集文字（文件管理器、表格）里检测器常把一个词切成一字一框：先把同一行里
    # 挨着的文字碎片并回去，再截断 —— 否则 200 个名额被单字吃光。
    kept = merge_text_fragments(kept)
    kept = drop_container_cells(kept)
    kept.sort(key=lambda c: (bool(c["text"]), c["conf"]), reverse=True)
    if stats is not None:
        stats["candidates"] = len(kept)
        stats["truncated"] = len(kept) > max_marks
    kept = kept[:max_marks]

    if not kept:
        return []

    heights = sorted(c["box"][3] - c["box"][1] for c in kept)
    band = max(8.0, heights[len(heights) // 2] * 0.6)
    # 按中心 y 排序后贪心分行：与本行首个框中心差不超过 band 的归同一行。
    by_y = sorted(kept, key=lambda c: (c["box"][1] + c["box"][3]) / 2)
    rows: list[list[dict]] = []
    row_y = None
    for c in by_y:
        cy = (c["box"][1] + c["box"][3]) / 2
        if row_y is None or cy - row_y > band:
            rows.append([c])
            row_y = cy
        else:
            rows[-1].append(c)
    ordered = [c for row in rows for c in sorted(row, key=lambda c: c["box"][0])]

    return [
        Mark(i, *c["box"], text=c["text"], conf=c["conf"], kind=c["kind"])
        for i, c in enumerate(ordered, start=1)
    ]


_TEXT_KINDS = ("text", "ocr")


def _joiner(a: str, b: str) -> str:
    """两段英文 / 数字之间补一个空格，中文之间不补。"""
    if a and b and a[-1].isascii() and a[-1].isalnum() and b[0].isascii() and b[0].isalnum():
        return " "
    return ""


def merge_text_fragments(cands: list[dict], *, gap_ratio: float = 0.45,
                         height_ratio: float = 1.6) -> list[dict]:
    """把同一行、高度相近、横向间距小于约半个字高的文字框并成一个。

    只并 kind 为 text / ocr 的框（图标、格子不动）。阈值取「半个字高」：
    同一个词里字与字的间距通常不到 0.2 个字高，而相邻的两个按钮 / 菜单项
    之间至少隔一个字宽，所以不会把「文件 编辑」并成一个。
    """
    def mergeable(c: dict) -> bool:
        # 单个英文字母 / 符号多半是工具栏按钮上的字形（粗体 B、斜体 I、对齐图标），
        # 按钮之间也挨得很近，并起来就点不到单个按钮了 —— 不并。
        t = c["text"]
        return c["kind"] in _TEXT_KINDS and bool(t) and not (len(t) == 1 and t.isascii())

    texts = [c for c in cands if mergeable(c)]
    others = [c for c in cands if not mergeable(c)]
    if len(texts) < 2:
        return cands
    texts = sorted(texts, key=lambda c: ((c["box"][1] + c["box"][3]) / 2, c["box"][0]))
    merged: list[dict] = []
    used = [False] * len(texts)
    # 按 x 排好的同行链：逐个往右找能接上的
    order = sorted(range(len(texts)), key=lambda i: texts[i]["box"][0])
    for i in order:
        if used[i]:
            continue
        cur = dict(texts[i])
        used[i] = True
        while True:
            x1, y1, x2, y2 = cur["box"]
            h = y2 - y1
            cy = (y1 + y2) / 2
            nxt = None
            for j in order:
                if used[j]:
                    continue
                bx1, by1, bx2, by2 = texts[j]["box"]
                bh = by2 - by1
                if max(h, bh) / max(1.0, min(h, bh)) > height_ratio:
                    continue
                if abs((by1 + by2) / 2 - cy) > 0.35 * min(h, bh):
                    continue
                gap = bx1 - x2
                if -0.3 * min(h, bh) <= gap <= gap_ratio * min(h, bh):
                    if nxt is None or bx1 < texts[nxt]["box"][0]:
                        nxt = j
            if nxt is None:
                break
            other = texts[nxt]
            used[nxt] = True
            bx1, by1, bx2, by2 = other["box"]
            cur = {
                **cur,
                "box": (min(x1, bx1), min(y1, by1), max(x2, bx2), max(y2, by2)),
                "text": cur["text"] + _joiner(cur["text"], other["text"]) + other["text"],
                "conf": max(cur["conf"], other["conf"]),
            }
        merged.append(cur)
    return others + merged


def drop_container_cells(cands: list[dict]) -> list[dict]:
    """几何补出来的「格子」如果把另外两个以上格子的中心包在里面，它是外框
    （或跨格的误检），不是一个可点的格子 —— 去掉。2048 棋盘上曾出现一个
    363x283 的格子横跨两行两列。"""
    cells = [c for c in cands if c["kind"] == "cell"]
    if len(cells) < 3:
        return cands
    centers = [((c["box"][0] + c["box"][2]) / 2, (c["box"][1] + c["box"][3]) / 2) for c in cells]
    drop = set()
    for i, c in enumerate(cells):
        x1, y1, x2, y2 = c["box"]
        inside = sum(1 for j, (cx, cy) in enumerate(centers)
                     if j != i and x1 < cx < x2 and y1 < cy < y2)
        if inside >= 2:
            drop.add(id(c))
    return [c for c in cands if id(c) not in drop]


def grid_cells(origin: tuple[int, int], size: tuple[int, int], n: int) -> dict[str, tuple[float, float, float, float]]:
    """把一块区域切成带名字的网格：列用字母（A, B, …），行用数字（1, 2, …）。

    ``n`` 是长边上的格数，短边按比例取（至少 1 格），所以格子接近正方形。
    返回 ``{"B3": (x1, y1, x2, y2)}``，屏幕绝对坐标。

    **为什么要网格**：检测器只框得出它认得的东西。游戏里的物品格、地图上的单位、
    自绘控件常常一个框都没有 —— 这时让模型选「哪一格」，再把那一格放大重新
    标注，两轮就能到像素级，不需要任何新模型。
    """
    if n <= 0:
        return {}
    w, h = size
    if w >= h:
        cols, rows = n, max(1, round(n * h / w))
    else:
        rows, cols = n, max(1, round(n * w / h))
    cols, rows = min(cols, 26), min(rows, 99)
    ox, oy = origin
    cw, ch = w / cols, h / rows
    return {
        f"{chr(65 + c)}{r + 1}": (ox + c * cw, oy + r * ch, ox + (c + 1) * cw, oy + (r + 1) * ch)
        for r in range(rows) for c in range(cols)
    }


def is_cell_ref(value: object) -> bool:
    """``"B3"`` 这种网格名（大小写都认）。"""
    if not isinstance(value, str):
        return False
    v = value.strip().upper()
    return len(v) >= 2 and "A" <= v[0] <= "Z" and v[1:].isdigit()


def _text_color(bgr: tuple[int, int, int]) -> tuple[int, int, int]:
    b, g, r = bgr
    return (0, 0, 0) if 0.299 * r + 0.587 * g + 0.114 * b > 150 else (255, 255, 255)


def _overlaps(r: tuple[int, int, int, int], others: list[tuple[int, int, int, int]]) -> bool:
    x1, y1, x2, y2 = r
    for a1, b1, a2, b2 in others:
        if x1 < a2 and a1 < x2 and y1 < b2 and b1 < y2:
            return True
    return False


def render(
    bgr: np.ndarray,
    marks: list[Mark],
    origin: tuple[int, int],
    *,
    max_side: int = 1600,
    upscale_max: float = 1.0,
    fmt: str = "jpeg",
    quality: int = 0,
    cells: dict[str, tuple[float, float, float, float]] | None = None,
) -> tuple[bytes, float]:
    """画标注图，返回 (图片字节, scale)。scale = 标注图像素 / 屏幕像素。

    先缩放、后画框：编号字号固定在标注图上约 14px 高，不会因为原图是 4K
    就缩成看不清的小点，也不会因为放大而糊成一团。

    编号标签的位置按「框外左上 → 框外右上 → 框外左下 → 框内左上」依次尝试，
    选第一个既不出图、也不压住已放标签的位置；都不行就放框内左上（宁可重叠
    也不能不标）。
    """
    h, w = bgr.shape[:2]
    scale = min(max_side / max(h, w), upscale_max)
    if scale <= 0:
        scale = 1.0
    if abs(scale - 1.0) > 1e-3:
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
        canvas = cv2.resize(bgr, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=interp)
    else:
        canvas = bgr.copy()
    ch, cw = canvas.shape[:2]
    ox, oy = origin

    def to_px(x: float, y: float) -> tuple[int, int]:
        return int(round((x - ox) * scale)), int(round((y - oy) * scale))

    font_scale, thick = 0.45, 1
    placed: list[tuple[int, int, int, int]] = []

    # 网格画在最底层：细线 + 每格左上角一个小灰标签。用白线再叠一条黑虚边，
    # 亮背景暗背景都看得见。
    if cells:
        overlay = canvas.copy()
        for name, (gx1, gy1, gx2, gy2) in cells.items():
            a1, b1 = to_px(gx1, gy1)
            a2, b2 = to_px(gx2, gy2)
            cv2.rectangle(overlay, (a1, b1), (a2, b2), (255, 255, 255), 1)
        canvas = cv2.addWeighted(overlay, 0.55, canvas, 0.45, 0)
        for name, (gx1, gy1, _gx2, _gy2) in cells.items():
            a1, b1 = to_px(gx1, gy1)
            (tw, th), base = cv2.getTextSize(name, FONT, 0.4, 1)
            r = (a1 + 1, b1 + 1, a1 + tw + 5, b1 + th + base + 3)
            cv2.rectangle(canvas, r[:2], r[2:], (40, 40, 40), -1)
            cv2.putText(canvas, name, (a1 + 3, r[3] - base - 1), FONT, 0.4, (255, 255, 255), 1, cv2.LINE_AA)
            placed.append(r)
    boxes_px: list[tuple[int, int, int, int, tuple[int, int, int]]] = []
    for m in marks:
        color = PALETTE[(m.id - 1) % len(PALETTE)]
        x1, y1 = to_px(m.x1, m.y1)
        x2, y2 = to_px(m.x2, m.y2)
        # 太小的框放大到至少 6px，否则缩小后的图上根本看不见框
        if x2 - x1 < 6:
            c = (x1 + x2) // 2
            x1, x2 = c - 3, c + 3
        if y2 - y1 < 6:
            c = (y1 + y2) // 2
            y1, y2 = c - 3, c + 3
        boxes_px.append((x1, y1, x2, y2, color))
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 2 if min(x2 - x1, y2 - y1) > 24 else 1)

    for m, (x1, y1, x2, y2, color) in zip(marks, boxes_px):
        label = str(m.id)
        (tw, th), base = cv2.getTextSize(label, FONT, font_scale, thick)
        lw, lh = tw + 4, th + base + 3
        options = [
            (x1, y1 - lh),          # 框外左上
            (x2 - lw, y1 - lh),     # 框外右上
            (x1, y2),               # 框外左下
            (x1, y1),               # 框内左上
        ]
        chosen = None
        for lx, ly in options:
            r = (lx, ly, lx + lw, ly + lh)
            if lx < 0 or ly < 0 or lx + lw > cw or ly + lh > ch:
                continue
            if _overlaps(r, placed):
                continue
            chosen = r
            break
        if chosen is None:
            lx = min(max(0, x1), max(0, cw - lw))
            ly = min(max(0, y1), max(0, ch - lh))
            chosen = (lx, ly, lx + lw, ly + lh)
        placed.append(chosen)
        lx, ly, rx, ry = chosen
        cv2.rectangle(canvas, (lx, ly), (rx, ry), color, -1)
        cv2.putText(canvas, label, (lx + 2, ry - base - 1), FONT, font_scale,
                    _text_color(color), thick, cv2.LINE_AA)

    if fmt == "png":
        ok, buf = cv2.imencode(".png", canvas)
    else:
        ok, buf = encode_jpeg(canvas, quality)
    if not ok:
        raise RuntimeError("标注图编码失败")
    return buf.tobytes(), scale


def jpeg_quality(default: int = 90) -> int:
    import os

    try:
        return max(50, min(int(os.environ.get("UI_LOCATE_JPEG_QUALITY") or default), 100))
    except ValueError:
        return default


def encode_jpeg(img: np.ndarray, quality: int = 0) -> tuple[bool, np.ndarray]:
    """给模型看的图统一这样编码：质量 90、色度**不降采样**（4:4:4）。

    视觉模型按像素尺寸计 token，和文件大小无关，所以压得更狠省不了钱；反过来，
    默认的 4:2:0 会把彩色小字和彩色编号标签的边缘糊掉（色度只剩四分之一分辨率），
    模型就认不清。4:4:4 + q90 比 4:2:0 + q88 大约多 20~40% 字节，本机回环传输
    这点体积可以忽略。``UI_LOCATE_JPEG_QUALITY`` 可改质量。
    """
    params = [cv2.IMWRITE_JPEG_QUALITY, int(quality) or jpeg_quality()]
    if hasattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR"):
        params += [cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444]
    return cv2.imencode(".jpg", img, params)


@dataclass
class MarkSet:
    id: int
    created: float
    marks: dict[int, Mark]
    origin: tuple[int, int]
    box: tuple[int, int, int, int] | None = None
    monitor: object = None
    meta: dict = field(default_factory=dict)
    cells: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)


class MarkStore:
    """记住最近一次的编号集合。

    编号只在**生成它的那一帧**上有意义：画面一变，#17 可能已经指向别的东西。
    所以每次生成都换一个新的集合号（单调递增、不复用）；act 时可以带上集合号，
    号不对就拒绝，而不是悄悄点到新画面上的另一个 #17。
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._current: MarkSet | None = None
        self._clock = clock

    def put(self, marks: list[Mark], origin: tuple[int, int], *, box=None, monitor=None,
            meta: dict | None = None, cells: dict | None = None) -> MarkSet:
        with self._lock:
            self._seq += 1
            self._current = MarkSet(self._seq, self._clock(), {m.id: m for m in marks},
                                    tuple(origin), box, monitor, dict(meta or {}), dict(cells or {}))
            return self._current

    def resolve_cell(self, name: object, set_id: object = None) -> tuple[tuple[float, float, float, float], MarkSet]:
        """``"B3"`` → (格子的屏幕矩形, 所属集合)。找不到抛 KeyError。"""
        s = self._check(set_id)
        key = str(name).strip().upper()
        if key not in s.cells:
            if not s.cells:
                raise KeyError("上一张标注图没有网格：调用 ui_locate_marks 时给 grid")
            raise KeyError(f"网格 {key} 不存在（当前集合 {s.id} 的网格是 A1..{list(s.cells)[-1]}）")
        return s.cells[key], s

    def _check(self, set_id: object) -> MarkSet:
        s = self._current
        if s is None:
            raise KeyError("还没有编号标注：先调用 ui_locate_marks")
        if set_id not in (None, "", 0, "0"):
            try:
                wanted = int(set_id)
            except (TypeError, ValueError):
                raise KeyError(f"mark_set 不是整数：{set_id!r}") from None
            if wanted != s.id:
                raise KeyError(
                    f"编号集合 {wanted} 已过期（当前是 {s.id}）：画面可能变了，重新调用 ui_locate_marks"
                )
        return s

    def current(self) -> MarkSet | None:
        return self._current

    def resolve(self, mark_id: object, set_id: object = None) -> tuple[Mark, MarkSet]:
        """``17`` / ``"17"`` / ``"#17"`` → (Mark, 所属集合)。找不到抛 KeyError（信息给模型看）。"""
        s = self._check(set_id)
        raw = str(mark_id).strip().lstrip("#")
        try:
            key = int(raw)
        except ValueError:
            raise KeyError(f"编号不是整数：{mark_id!r}") from None
        m = s.marks.get(key)
        if m is None:
            raise KeyError(f"编号 #{key} 不存在（当前集合 {s.id} 有 1..{len(s.marks)}）")
        return m, s

    def age_s(self) -> float | None:
        s = self._current
        return None if s is None else round(self._clock() - s.created, 2)


def is_mark_ref(value: object) -> bool:
    """``"#17"`` 这种写法才算编号引用；裸数字字符串可能是屏幕上的文字（页码）。"""
    return isinstance(value, str) and value.strip().startswith("#") and value.strip()[1:].isdigit()
