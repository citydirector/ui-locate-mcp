"""用 Windows.Graphics.Capture 抓单个窗口（被挡住也能抓，GPU 渲染的游戏也不黑）。

**为什么不只用 PrintWindow**：PrintWindow 是让窗口「把自己画一遍」给我们，
D3D / Vulkan 渲染的游戏常常画不出来（全黑），只能退回「按屏抓再裁」，而那
要求窗口没被挡。WGC 直接从合成器（DWM）拿这个窗口的画面，被别的窗口盖住
也能拿到，GPU 渲染的内容也在。

**依赖**：``windows-capture``（Rust 实现，自带 WGC 窗口捕获）。它的元数据里
要求 ``opencv-python``，和我们的 ``opencv-python-headless`` 冲突（两个包都提供
``cv2``），所以插件用 ``--no-deps`` 单独装它；它自己只需要 numpy 和 cv2，
headless 版就满足。没装 → 这一层直接跳过，退回 PrintWindow。

**会话**：建一个 WGC 会话要几十毫秒，第一帧再等一下。所以每个窗口保持一个
常驻会话，回调里只留最新一帧；连续抓（watch 录制、wait 等静止）几乎零开销。
空闲 ``IDLE_S`` 秒没人要帧就关掉。WGC 只在画面变化时送帧，所以「最新一帧」
在画面静止时就是当前画面。

**坐标**：WGC 的窗口帧 = DWM 的可见外框（``DWMWA_EXTENDED_FRAME_BOUNDS``，
物理像素），带标题栏和边框。我们裁成客户区，和 PrintWindow 的约定一致：
图左上角 = 客户区左上角的屏幕坐标。
"""

from __future__ import annotations

import sys
import threading
import time

import numpy as np

_IS_WINDOWS = sys.platform == "win32"

IDLE_S = 15.0           # 空闲多久关会话
FIRST_FRAME_S = 1.0     # 新会话等第一帧的上限
SIZE_TOLERANCE = 2      # 帧尺寸和外框尺寸差几像素以内算对得上


class WgcUnavailable(RuntimeError):
    """没装 windows-capture / 系统不支持 / 窗口抓不到（最小化等）。"""


def client_crop_box(frame_wh: tuple[int, int], bounds: tuple[int, int, int, int],
                    client: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """WGC 窗口帧里客户区的那块 (x0, y0, x1, y1)。

    frame_wh: 帧的 (w, h)；bounds: 可见外框 (left, top, right, bottom)，屏幕坐标；
    client: 客户区 (x, y, w, h)，屏幕坐标。帧和外框对不上（DPI 虚拟化之类）→ None，
    调用方整帧用、原点取外框左上角。
    """
    fw, fh = frame_wh
    bl, bt, br, bb = bounds
    if abs((br - bl) - fw) > SIZE_TOLERANCE or abs((bb - bt) - fh) > SIZE_TOLERANCE:
        return None
    cx, cy, cw, ch = client
    x0, y0 = max(0, cx - bl), max(0, cy - bt)
    x1, y1 = min(fw, cx - bl + cw), min(fh, cy - bt + ch)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return x0, y0, x1, y1


def border_param(build: int) -> bool | None:
    """Win11（22000+）可以关掉黄框；Win10 上传 False 会报错，只能不传。"""
    return False if build >= 22000 else None


def _windows_build() -> int:
    try:
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return 0


def available() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        import windows_capture  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class _Session:
    def __init__(self, hwnd: int) -> None:
        from windows_capture import WindowsCapture

        self.hwnd = hwnd
        self.lock = threading.Lock()
        self.frame: np.ndarray | None = None
        self.frame_t = 0.0
        self.got = threading.Event()
        self.closed = False
        self.error: str | None = None
        self.used = time.monotonic()
        cap = WindowsCapture(cursor_capture=False, draw_border=border_param(_windows_build()),
                             window_hwnd=int(hwnd))

        @cap.event
        def on_frame_arrived(frame, control):  # noqa: ANN001
            if self.closed:
                control.stop()
                return
            buf = frame.frame_buffer
            bgr = np.ascontiguousarray(buf[:, :, :3])     # BGRA → BGR，同时拷出原生缓冲
            with self.lock:
                self.frame, self.frame_t = bgr, time.monotonic()
            self.got.set()

        @cap.event
        def on_closed():
            self.closed = True
            self.got.set()

        self.control = cap.start_free_threaded()

    def latest(self, wait_s: float) -> np.ndarray:
        self.used = time.monotonic()
        if self.frame is None:
            self.got.wait(wait_s)
        with self.lock:
            frame = self.frame
        if frame is None:
            raise WgcUnavailable("WGC 没送来画面（窗口最小化 / 受保护内容 / 系统不支持）"
                                 if not self.closed else "WGC 会话被关闭（窗口关了？）")
        return frame

    def stop(self) -> None:
        self.closed = True
        try:
            self.control.stop()
        except Exception:  # noqa: BLE001
            pass


class Pool:
    """每个 hwnd 一个常驻会话。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, _Session] = {}

    def _reap(self) -> None:
        now = time.monotonic()
        for hwnd, s in list(self._sessions.items()):
            if s.closed or now - s.used > IDLE_S:
                s.stop()
                self._sessions.pop(hwnd, None)

    def frame(self, hwnd: int) -> np.ndarray:
        if not available():
            raise WgcUnavailable("没装 windows-capture（插件会用 --no-deps 补装）")
        with self._lock:
            self._reap()
            s = self._sessions.get(int(hwnd))
            if s is None:
                try:
                    s = _Session(int(hwnd))
                except Exception as error:  # noqa: BLE001
                    raise WgcUnavailable(f"建 WGC 会话失败：{error}") from error
                self._sessions[int(hwnd)] = s
        try:
            return s.latest(FIRST_FRAME_S)
        except WgcUnavailable:
            with self._lock:
                s.stop()
                self._sessions.pop(int(hwnd), None)
            raise

    def release(self) -> None:
        with self._lock:
            for s in self._sessions.values():
                s.stop()
            self._sessions.clear()

    def status(self) -> dict:
        with self._lock:
            return {"available": available(), "sessions": sorted(self._sessions)}


POOL = Pool()


if _IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wt

    _dwm = ctypes.WinDLL("dwmapi")
    _dwm.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
    DWMWA_EXTENDED_FRAME_BOUNDS = 9


def frame_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """DWM 可见外框 (left, top, right, bottom)，物理像素（服务进程是 Per-Monitor V2）。"""
    r = wt.RECT()
    hr = _dwm.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r),
                                    ctypes.sizeof(r))
    if hr != 0:
        raise WgcUnavailable(f"DwmGetWindowAttribute 失败 hr=0x{hr & 0xFFFFFFFF:08x}")
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def grab_client(hwnd: int, client: tuple[int, int, int, int]) -> tuple[np.ndarray, tuple[int, int], dict]:
    """WGC 抓窗口并裁成客户区 → (BGR, 原点屏幕坐标, 附加信息)。"""
    frame = POOL.frame(hwnd)
    bounds = frame_bounds(hwnd)
    box = client_crop_box((frame.shape[1], frame.shape[0]), bounds, client)
    if box is None:
        return frame.copy(), (bounds[0], bounds[1]), {"cropped": False, "bounds": bounds}
    x0, y0, x1, y1 = box
    return (np.ascontiguousarray(frame[y0:y1, x0:x1]), (bounds[0] + x0, bounds[1] + y0),
            {"cropped": True})
