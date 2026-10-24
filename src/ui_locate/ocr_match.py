"""把整页 OCR 结果与检测框做空间匹配，给每个元素附上文字标签。

为什么不能逐框 OCR：实测 12 个裁片逐个 OCR 要 1389.8ms，整页一次只要 290ms。
每次 OCR 调用有固定开销（模型调度、图像预处理），批量才是对的。

匹配策略：
  对每个 OCR 文本框，计算它的中心点落在哪个检测框内（或与哪个框 IoU 最高），
  把文本贴上去。一个框可以拿到多段文本（比如"保存" + "Ctrl+S"）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OcrLine:
    text: str
    score: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


def _contains(box: dict, x: float, y: float) -> bool:
    return box["x1"] <= x <= box["x2"] and box["y1"] <= y <= box["y2"]


def _overlap_ratio(box: dict, line: OcrLine) -> float:
    """文本框有多大比例落在检测框内。"""
    if line.area <= 0:
        return 0.0
    ix1 = max(box["x1"], line.x1)
    iy1 = max(box["y1"], line.y1)
    ix2 = min(box["x2"], line.x2)
    iy2 = min(box["y2"], line.y2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    return inter / line.area


def attach_text(
    items: list[dict], lines: list[OcrLine], min_overlap: float = 0.5
) -> list[dict]:
    """给每个检测框附上 text 字段。原地修改并返回 items。"""
    for it in items:
        it["text"] = ""
        it["text_score"] = 0.0

    # 每个文本框归给「包含它中心点」的最小面积检测框；没有则退回重叠率最高的
    buckets: dict[int, list[tuple[float, str]]] = {}
    orphans: list[OcrLine] = []

    for line in lines:
        best_i, best_area = -1, float("inf")
        for i, it in enumerate(items):
            if _contains(it, line.cx, line.cy):
                area = it["w"] * it["h"]
                if area < best_area:
                    best_i, best_area = i, area

        if best_i < 0:
            best_i, best = -1, 0.0
            for i, it in enumerate(items):
                r = _overlap_ratio(it, line)
                if r > best:
                    best_i, best = i, r
            if best < min_overlap:
                orphans.append(line)
                continue

        buckets.setdefault(best_i, []).append((line.cy, line.text, line.score))

    for i, entries in buckets.items():
        entries.sort(key=lambda e: e[0])
        it = items[i]
        it["text"] = " ".join(e[1] for e in entries).strip()
        it["text_score"] = round(max(e[2] for e in entries), 3)

    # 没被任何检测框吃下的文字，作为纯文本元素补进列表，
    # 这样"按文字找位置"的查询不会因为漏检而失效
    for line in orphans:
        items.append(
            {
                "x1": round(line.x1, 1),
                "y1": round(line.y1, 1),
                "x2": round(line.x2, 1),
                "y2": round(line.y2, 1),
                "cx": round(line.cx, 1),
                "cy": round(line.cy, 1),
                "w": round(line.x2 - line.x1, 1),
                "h": round(line.y2 - line.y1, 1),
                "conf": round(line.score, 3),
                "text": line.text,
                "text_score": round(line.score, 3),
                "text_only": True,
            }
        )

    return items


def _strip_icon_noise(t: str) -> str:
    """去掉 OCR 把图标误认成的字符。

    侧栏按钮前面有个图标，OCR 经常把它读成 `8`、`$`、`④`、`口`、`▣` 之类，
    于是「插件」被识别成「8 插件」「$ 插件」「④插件」。这些噪声字符会让
    "完全相等"永远不成立，精确匹配降级成"包含"，排序就输给「+ 添加插件」。

    做法：去掉首尾的非中日韩、非字母数字的字符，以及**单独出现**的单字符
    数字/符号前缀。只剥首尾，不动中间 —— 中间的多半是真实内容。
    """
    s = t.strip()
    if not s:
        return s

    def is_noise(ch: str) -> bool:
        # 图标被认成的常见形态：ASCII 标点符号、数字、私用区/几何符号
        if ch.isspace():
            return True
        if ch.isdigit() and len(s) <= 6:
            return True  # 短文本里的孤立数字几乎都是图标
        code = ord(ch)
        if code < 0x20:
            return True
        if 0x2000 <= code <= 0x2BFF:  # 各类符号、箭头、几何形状
            return True
        if 0xE000 <= code <= 0xF8FF:  # 私用区
            return True
        if 0xFF00 <= code <= 0xFF65:  # 全角标点
            return ch in "　、。，．：；！？"
        if ch in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~":
            return True
        return False

    lo, hi = 0, len(s) - 1
    while lo <= hi and is_noise(s[lo]):
        lo += 1
    while hi >= lo and is_noise(s[hi]):
        hi -= 1
    core = s[lo : hi + 1]
    # 剥完什么都不剩就退回原串，避免把「8」这种真的全是符号的识别结果抹掉
    return core if core else s


def find(items: list[dict], query: str, limit: int = 5) -> list[dict]:
    """按文字模糊查找元素，返回最匹配的几个。

    排序规则（这个顺序是踩过坑才定下来的）：
      1. 完全相等
      2. 以查询词开头 / 结尾（如查「插件」命中「8 插件」）
      3. 包含查询词
      然后同档内按「文字长度与查询词的长度差」升序 —— 越接近越优先 ——
      最后才按 conf 降序。

    匹配前先剥掉图标被误认成的字符（见 `_strip_icon_noise`），否则
    「8 插件」永远进不了"完全相等"档。

    为什么长度比 conf 更重要：实测查「插件」时，正文里的一句
    「这次"插件"没有点中…」的 OCR 置信度是 0.99，而真正的按钮
    「8 插件」只有 0.46。只看 conf 会把长段落排在按钮前面，点下去
    落在正文中间。控件名的特点是**短**，所以长度差是最可靠的信号。
    """
    q = query.strip().lower()
    if not q:
        return []

    exact: list[dict] = []
    starts: list[dict] = []
    contains: list[dict] = []
    for it in items:
        raw = (it.get("text") or "").strip()
        if not raw:
            continue
        cleaned = _strip_icon_noise(raw).lower()
        t = cleaned or raw.lower()
        if t == q:
            exact.append(it)
        elif t.startswith(q) or t.endswith(q):
            starts.append(it)
        elif q in t or t in q:
            contains.append(it)

    def rank(d: dict) -> tuple[int, int, float]:
        # 用剥噪声后的长度算差，否则「8 插件」(4字) 会比「插件」(2字) 吃亏
        raw = (d.get("text") or "").strip()
        core = _strip_icon_noise(raw)
        return (abs(len(core) - len(q)), len(raw), -d.get("conf", 0.0))

    for group in (exact, starts, contains):
        group.sort(key=rank)
    return (exact + starts + contains)[:limit]
