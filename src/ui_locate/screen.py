"""屏幕几何与抓屏：多显示器、DPI、虚拟桌面坐标系。

**为什么要有这一层**

检测器给的是「图内像素坐标」，点击要的是「屏幕绝对坐标」。中间只要有任何
一次隐式换算 —— DPI 缩放、显示器偏移、负坐标裁剪 —— 坐标就会偏，而且偏得
小的时候极难发现（点偏 20px，肉眼看截图像是"差不多"）。

多显示器下副屏原点常常是负数（主屏在右、副屏在左时的典型布局），
这是最容易翻车的地方 —— 开发机上实测过：

  - `ImageGrab.grab(bbox=<含负坐标的矩形>)` 直接对负坐标 bbox 截图会**全黑**
    （max<8），看起来像"副屏没接"。
  - 但 `ImageGrab.grab(all_screens=True)` 能正常抓到整个虚拟桌面，
    负坐标区域也有内容（实测副屏区域 max=255, mean=31.8）。

所以本模块的抓屏策略是：**永远只抓一次完整虚拟桌面，单屏靠裁剪**。
一次抓取、一个坐标系，避开了负坐标 bbox 的坑，也不必担心两次抓取之间
屏幕内容变了导致坐标错位。

**坐标系约定**（务必守住）

  - `Monitor.x/y` 是屏幕绝对坐标，取虚拟桌面坐标系，**可以为负**。
  - `grab()` 返回 `(bgr, origin)`，`origin` 是这张图的左上角在屏幕绝对
    坐标系里的位置。
  - 因此：`屏幕绝对坐标 = origin + 图内坐标`。
  - 交给 `cua_driver_native__click(scope="desktop", x=, y=)` 的必须是
    **屏幕绝对坐标**。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterable

import numpy as np

_dpi_set = False


def set_dpi_aware() -> str:
    """声明 Per-Monitor V2 DPI 感知，跟 cua_driver 对齐。

    不声明的话，本进程看到的是「逻辑像素」，而 cua_driver 用「物理像素」。
    在 150% 缩放的屏上两者差 1.5 倍 —— 抓到的图和点击坐标会整体错位。
    开发机上实测缩放是 1x（物理 = 逻辑），但不能假定换台机器还是这样，
    所以这一步必须做。
    """
    global _dpi_set
    if _dpi_set:
        return "already"
    mode = "none"
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            mode = "PerMonitorV2"
    except Exception:
        pass
    if mode == "none":
        try:
            if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
                mode = "PerMonitor"
        except Exception:
            pass
    if mode == "none":
        try:
            if ctypes.windll.user32.SetProcessDPIAware():
                mode = "System"
        except Exception:
            pass
    _dpi_set = True
    return mode


@dataclass(frozen=True)
class Monitor:
    """一块显示器。x/y 是屏幕绝对坐标，可能为负。"""

    index: int
    device: str
    primary: bool
    x: int
    y: int
    w: int
    h: int

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "device": self.device,
            "primary": self.primary,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
        }


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(_RECT), wintypes.LPARAM
)
_MONITORINFOF_PRIMARY = 0x1


def list_monitors() -> list[Monitor]:
    """枚举所有显示器，按「主屏在前，其余按 x 从左到右」排序。

    排序是为了让 index 稳定：调用方说「第 0 块屏」时，拿到的一定是主屏，
    不依赖 EnumDisplayMonitors 的返回顺序（那个顺序不保证）。
    """
    set_dpi_aware()
    found: list[Monitor] = []

    def _cb(hmon, _hdc, _rect, _lparam):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(
                Monitor(
                    index=-1,
                    device=info.szDevice,
                    primary=bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                    x=int(r.left),
                    y=int(r.top),
                    w=int(r.right - r.left),
                    h=int(r.bottom - r.top),
                )
            )
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_cb), 0)

    found.sort(key=lambda m: (not m.primary, m.x, m.y))
    return [
        Monitor(i, m.device, m.primary, m.x, m.y, m.w, m.h)
        for i, m in enumerate(found)
    ]


def virtual_rect() -> tuple[int, int, int, int]:
    """整个虚拟桌面的 (x, y, w, h)。x/y 可能为负。"""
    set_dpi_aware()
    u = ctypes.windll.user32
    return (
        u.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
        u.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
        u.GetSystemMetrics(78),  # SM_CXVIRTUALSCREEN
        u.GetSystemMetrics(79),  # SM_CYVIRTUALSCREEN
    )


def resolve_monitor(spec: object) -> Monitor | None:
    """把各种写法解析成一块显示器。

    接受：
      None / "all" / ""      → None（表示整个虚拟桌面，不裁）
      "primary"              → 主屏
      0 / "0" / 1 / "1"      → 按 index
      "\\\\.\\DISPLAY2"       → 按设备名
    """
    mons = list_monitors()
    if not mons:
        return None

    if spec is None:
        return None
    if isinstance(spec, str) and spec.strip().lower() in ("", "all", "*", "every"):
        return None
    if isinstance(spec, str) and spec.strip().lower() == "primary":
        return next((m for m in mons if m.primary), mons[0])

    if isinstance(spec, str) and spec.strip().isdecimal():
        spec = int(spec.strip())
    if isinstance(spec, int):
        return mons[spec] if 0 <= spec < len(mons) else None

    if isinstance(spec, str):
        for m in mons:
            if m.device.lower() == spec.strip().lower():
                return m

    return None


def grab_region(rect: tuple[int, int, int, int]) -> np.ndarray:
    """抓虚拟桌面的一块区域，返回 BGR（GDI BitBlt 路线）。

    rect 是屏幕绝对坐标 (x, y, w, h)，x/y 可以为负。保留它是给只想要一块
    任意矩形、不关心后端的调用方；按屏抓图请用 `grab`，它会按设置选
    DXGI / WGC / GDI（见 `ui_locate.capture`）。
    """
    set_dpi_aware()
    from ui_locate.capture import Region, _grab_gdi

    x, y, w, h = rect
    return _grab_gdi(Region("", x, y, w, h))


def _grab_monitor(m: Monitor) -> np.ndarray:
    from ui_locate.capture import CAPTURER, Region

    return CAPTURER.grab(Region(m.device, m.x, m.y, m.w, m.h)).bgr


def grab(monitor: object = None) -> tuple[np.ndarray, tuple[int, int]]:
    """抓一块屏（或整个虚拟桌面），返回 (BGR, origin)。

    origin 是这张图左上角的**屏幕绝对坐标**。调用方算出的图内坐标加上
    origin 才是能交给 cua_driver 的坐标。

    参数 monitor：见 `resolve_monitor`。默认整个虚拟桌面。也可以写
    ``"window:标题"`` / ``"hwnd:123"`` 只抓一个窗口（见 `ui_locate.window`）。

    抓屏后端由 `ui_locate.capture` 决定（默认 auto：DXGI → WGC → GDI）。
    整个虚拟桌面按屏分别抓、再贴进一张画布：DXGI / WGC 本来就是一块输出
    一台相机，显示器之间不存在的空白保持全黑，和 GDI 抓出来的样子一致。
    """
    set_dpi_aware()
    from ui_locate import window

    if window.is_window_spec(monitor):
        # "window:标题" / "hwnd:123"：只抓这个窗口的客户区（被挡住也能抓）
        return window.grab(monitor)
    m = resolve_monitor(monitor)
    if m is not None:
        return _grab_monitor(m), (m.x, m.y)
    vx, vy, vw, vh = virtual_rect()
    mons = list_monitors()
    canvas = np.zeros((vh, vw, 3), dtype=np.uint8)
    for mon in mons:
        img = _grab_monitor(mon)
        x0, y0 = mon.x - vx, mon.y - vy
        h = min(img.shape[0], vh - y0)
        w = min(img.shape[1], vw - x0)
        canvas[y0:y0 + h, x0:x0 + w] = img[:h, :w]
    return canvas, (vx, vy)


def iter_monitors() -> Iterable[Monitor]:
    return list_monitors()


def describe() -> dict:
    """给诊断用的完整几何信息。"""
    mode = set_dpi_aware()
    vx, vy, vw, vh = virtual_rect()
    from ui_locate.capture import CAPTURER

    return {
        "dpi_awareness": mode,
        "capture": CAPTURER.status(),
        "virtual": {"x": vx, "y": vy, "w": vw, "h": vh},
        "monitors": [m.as_dict() for m in list_monitors()],
    }
