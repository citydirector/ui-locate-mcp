"""无损帧记忆：把抓到过的画面原样存下来，按回合编号索引。

## 为什么要有这个

原来每次调用都是「截一张 → 检测 → OCR → 扔掉」。这有两个代价：

1. **同一张画面被反复截**。实测过一次操作里 `get_window_state` 已经返回了
   截图，却又 `read_image` 重读同一张 —— 每张图付两遍编解码与 token。
2. **跨轮没有任何状态**。验证「刚才那一下生效了吗」必须把 before 和 after
   都留在同一次调用的手里；调用一结束，前后对照就没了。

这两点来自同一个设计缺口：**只传结论，不留证据**。存「按钮在 (147,170)」这种
结论，等于提前替未来的自己判断了「哪些像素以后会有用」—— 而那一刻根本不知道。

这个模块只做一件事：**存帧，不存结论**。

## 回合编号怎么来的

一次 `/act` 或 `/detect` 算一个回合（turn），回合内可以有多帧（frame）：
第 0 帧是动作之前，第 1 帧是动作之后。这样「比一比动作前后」不需要调用方
自己保留两张图 —— 它们本来就在同一个回合里。

## 存什么、存多少

存**原始像素**（numpy 数组），不存 PNG。理由：PNG 编码一张 2880x1800 要
100~200ms，而这里每一帧都在请求路径上，延迟比内存更贵。代价是每帧约
15.5MB，所以有字节上限，超了从最老的回合开始丢。

上限默认 256MB（约 16 帧 2880x1800，或 60 帧 1920x1080），可用
`UI_LOCATE_FRAME_MB` 调。**这个值就是「能回溯多远」与「常驻内存多大」的
直接交易**，没有免费的选择。

## 取用

    store = FrameStore()
    turn = store.begin_turn()
    ref = store.put(turn, bgr, origin=(0, 0), label="before")
    ...
    frame = store.get(ref.turn, ref.frame)

线程安全：服务是多线程的（`ThreadingHTTPServer`），所有公开方法都在锁里。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# 单次 crop 输出的长边上限。给模型的图太大没有意义 —— 上下文装不下，
# 而且 MCP 传输层也有大小限制。
_CROP_MAX_SIDE = 1600


def _default_bytes_cap() -> int:
    """帧缓存的上限字节数。默认 256MB，可用环境变量改。"""
    raw = os.environ.get("UI_LOCATE_FRAME_MB", "").strip()
    if raw:
        try:
            return max(16, int(float(raw) * 1024 * 1024))
        except ValueError:
            pass
    return 256 * 1024 * 1024


@dataclass(frozen=True)
class FrameRef:
    """指向某一帧的坐标。回合号 + 回合内序号。"""

    turn: int
    frame: int

    def as_dict(self) -> dict[str, int]:
        return {"turn": self.turn, "frame": self.frame}


@dataclass
class Frame:
    """一帧的原始数据与元信息。"""

    ref: FrameRef
    bgr: np.ndarray
    origin: tuple[int, int]
    label: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def size(self) -> tuple[int, int]:
        """(宽, 高)。"""
        return int(self.bgr.shape[1]), int(self.bgr.shape[0])

    @property
    def bytes(self) -> int:
        return int(self.bgr.nbytes)

    def as_dict(self) -> dict[str, Any]:
        w, h = self.size
        return {
            "turn": self.ref.turn,
            "frame": self.ref.frame,
            "label": self.label,
            "size": [w, h],
            "origin": [int(self.origin[0]), int(self.origin[1])],
            "age_s": round(time.time() - self.ts, 2),
        }


@dataclass
class _Turn:
    number: int
    frames: list[Frame] = field(default_factory=list)
    opened_at: float = field(default_factory=time.time)

    @property
    def bytes(self) -> int:
        return sum(f.bytes for f in self.frames)


class FrameStore:
    """帧的环形缓存。超过上限就从最老的回合整批丢弃。"""

    def __init__(
        self,
        *,
        max_turns: int = 40,
        max_frames_per_turn: int = 8,
        max_bytes: int | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._turns: list[_Turn] = []          # 按回合号升序
        self._next_turn = 1
        self.max_turns = max_turns
        self.max_frames_per_turn = max_frames_per_turn
        self.max_bytes = _default_bytes_cap() if max_bytes is None else max_bytes
        self._peak_bytes = 0

    # --- 写入 ---------------------------------------------------------------

    def begin_turn(self) -> int:
        """开一个新回合，返回回合号。

        回合号单调递增且永不复用 —— 复用会让「第 3 回合」在不同时刻指不同
        画面，回溯就变得不可信了。
        """
        with self._lock:
            number = self._next_turn
            self._next_turn += 1
            self._turns.append(_Turn(number=number))
            self._evict()
            return number

    def put(
        self,
        turn: int,
        bgr: np.ndarray,
        origin: tuple[int, int] = (0, 0),
        label: str = "",
    ) -> FrameRef | None:
        """存一帧。回合号不存在（已被淘汰）或该回合帧数满了就返回 None。

        **不拷贝** bgr：调用方拿到的一般是 `screen.grab()` 刚返回的新数组，
        拷一份只是白白多付一次 15MB 内存。调用方若复用同一块缓冲区，需要
        自己 copy 后再传进来。
        """
        with self._lock:
            target = next((t for t in self._turns if t.number == turn), None)
            if target is None:
                return None
            if len(target.frames) >= self.max_frames_per_turn:
                return None
            index = len(target.frames)
            frame = Frame(
                ref=FrameRef(turn=turn, frame=index),
                bgr=bgr,
                origin=(int(origin[0]), int(origin[1])),
                label=label,
            )
            target.frames.append(frame)
            used = sum(t.bytes for t in self._turns)
            self._peak_bytes = max(self._peak_bytes, used)
            self._evict()
            return frame.ref

    def _evict(self) -> None:
        """按上限淘汰。调用方必须已持有锁。"""
        # 回合数超了
        while len(self._turns) > self.max_turns:
            self._turns.pop(0)
        # 字节超了：从最老的整批丢。丢单帧会让「第 3 回合第 1 帧」变成
        # 「第 3 回合第 0 帧」，序号错位比少一帧更难查。
        while len(self._turns) > 1 and sum(t.bytes for t in self._turns) > self.max_bytes:
            self._turns.pop(0)
        # 只剩一个回合还超，就丢它里面的最老帧
        while (
            len(self._turns) == 1
            and self._turns[0].frames
            and self._turns[0].bytes > self.max_bytes
        ):
            self._turns[0].frames.pop(0)

    # --- 读取 ---------------------------------------------------------------

    def get(self, turn: int, frame: int = 0) -> Frame | None:
        with self._lock:
            for t in self._turns:
                if t.number == turn:
                    if 0 <= frame < len(t.frames):
                        return t.frames[frame]
                    if frame < 0 and t.frames:
                        # 负索引从最新的往前数，和 Python 列表一致
                        idx = len(t.frames) + frame
                        return t.frames[idx] if 0 <= idx < len(t.frames) else None
                    return None
            return None

    def latest(self) -> Frame | None:
        with self._lock:
            for t in reversed(self._turns):
                if t.frames:
                    return t.frames[-1]
            return None

    def latest_turn(self) -> int:
        with self._lock:
            return self._next_turn - 1

    def turns_of(self, turn: int) -> list[Frame]:
        """取某个回合里的全部帧（没有就是空表）。

        存在的理由很具体：调用方要回答「这一回合到底抓到没抓到帧」，以便
        决定要不要补抓一张。用 ``get(turn, 0) is None`` 判断是错的 ——
        回合存在但零帧，和回合已被淘汰，都会返回 None，两者含义完全不同。
        """
        with self._lock:
            for t in self._turns:
                if t.number == turn:
                    return list(t.frames)
            return []

    def turns(self, limit: int = 20) -> list[dict[str, Any]]:
        """列出最近几个回合的摘要。

        给模型看的：它得先知道「有哪些帧可取」才能决定取哪一帧。只报回合号
        和帧数，不报像素 —— 摘要本身不该变成新的上下文负担。
        """
        with self._lock:
            out = []
            for t in reversed(self._turns[-limit:]):
                out.append({
                    "turn": t.number,
                    "frames": [f.as_dict() for f in t.frames],
                    "age_s": round(time.time() - t.opened_at, 2),
                    "bytes": t.bytes,
                })
            return out

    def stats(self) -> dict[str, Any]:
        with self._lock:
            used = sum(t.bytes for t in self._turns)
            frames = sum(len(t.frames) for t in self._turns)
            return {
                "turns": len(self._turns),
                "frames": frames,
                "bytes": used,
                "peak_bytes": self._peak_bytes,
                "max_bytes": self.max_bytes,
                "max_turns": self.max_turns,
                "max_frames_per_turn": self.max_frames_per_turn,
            }

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()
            self._peak_bytes = 0

    # --- 空间上的检查 -------------------------------------------------------

    def crop(
        self,
        frame: Frame,
        box: tuple[int, int, int, int] | None = None,
        scale: float | None = None,
        max_side: int = _CROP_MAX_SIDE,
    ):
        """裁一块并放大，返回 (bgr, meta)。

        `box` 是**屏幕绝对坐标** (x1, y1, x2, y2)，不是图内坐标 —— 调用方
        手里一律是绝对坐标（元素坐标、点击点都是），在这里换算一次比让它
        自己减 origin 更不容易错。

        `scale=None` 表示自动：把裁出来的那块放大到长边正好 `max_side`，
        但不放大超过 6 倍（再大只是插值出来的糊图，对读字没有帮助）。
        """
        import cv2  # 局部导入：只有真要产出图像时才需要

        ox, oy = frame.origin
        h, w = frame.bgr.shape[:2]

        if box is None:
            x1, y1, x2, y2 = ox, oy, ox + w, oy + h
        else:
            x1, y1, x2, y2 = (int(v) for v in box)

        # 绝对坐标 → 图内坐标
        lx1, ly1 = x1 - ox, y1 - oy
        lx2, ly2 = x2 - ox, y2 - oy

        # 裁到图内。越界的部分直接截掉而不是报错 —— 调用方想看的可能恰好
        # 压着屏幕边缘，为此让整次调用失败不值得。
        lx1, lx2 = max(0, min(lx1, lx2)), min(w, max(lx1, lx2))
        ly1, ly2 = max(0, min(ly1, ly2)), min(h, max(ly1, ly2))
        if lx2 - lx1 < 2 or ly2 - ly1 < 2:
            return None, {
                "error": "crop 的区域落在画面之外",
                "asked": [x1, y1, x2, y2],
                "frame_size": [w, h],
                "frame_origin": [ox, oy],
            }

        piece = frame.bgr[ly1:ly2, lx1:lx2]
        ph, pw = piece.shape[:2]

        if scale is None:
            long_edge = max(pw, ph)
            scale = max_side / long_edge if long_edge else 1.0
            scale = min(scale, 6.0)
        scale = max(0.1, float(scale))

        if abs(scale - 1.0) > 0.01:
            # INTER_NEAREST 而不是 CUBIC：放大文字时最近邻不会引入新的灰度，
            # 边缘更硬更像原图；CUBIC 会把笔画糊成一片，OCR 与读图都更差。
            piece = cv2.resize(
                piece, (max(1, round(pw * scale)), max(1, round(ph * scale))),
                interpolation=cv2.INTER_NEAREST,
            )

        out_h, out_w = piece.shape[:2]
        return piece, {
            "turn": frame.ref.turn,
            "frame": frame.ref.frame,
            "label": frame.label,
            # 这块裁剪对应屏幕上的哪一段 —— 调用方要据此把图里的位置换算回
            # 屏幕坐标（比如「第 3 个按钮在放大图 x=412」→ 屏幕 x = ...）。
            "box": [ox + lx1, oy + ly1, ox + lx2, oy + ly2],
            "scale": round(scale, 3),
            "size": [out_w, out_h],
            # 缩放是均匀的，一个数够用；同时给出也更省得调用方自己算
            "origin": [ox + lx1, oy + ly1],
        }


__all__ = ["Frame", "FrameRef", "FrameStore"]
