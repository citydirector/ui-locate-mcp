"""抓屏后端：DXGI Desktop Duplication / Windows Graphics Capture / GDI BitBlt。

**为什么不止一种**

三种方式各有一块别人做不到的地盘，没有哪一种能通吃：

| 后端 | 怎么抓 | 强项 | 会在哪里翻车 |
|------|--------|------|--------------|
| ``dxgi`` | Desktop Duplication，直接拿 DWM 合成好的显示器画面（GPU 内存） | 最快（1080p 一帧毫秒级）；独占全屏的 D3D 游戏也能抓到 | 混合显卡笔记本：抓屏进程跑在独显、屏幕接在核显上时会报 ``DXGI_ERROR_UNSUPPORTED``；UAC 安全桌面、切分辨率、全屏切换时会 ``ACCESS_LOST`` 需要重建 |
| ``wgc`` | Windows Graphics Capture（WinRT） | 同样走 GPU，混合显卡更省心；Win10 1903+ | 需要 ``dxcam[winrt]`` 额外依赖；Win10 上会有黄色边框 |
| ``gdi`` | ``BitBlt``（PIL ``ImageGrab``），从屏幕 DC 拷贝 | 哪台 Windows 都能跑，没有设备丢失、没有「没新帧」这类状态，所以常被说成「兼容性更好」 | 独占全屏 / 硬件加速的游戏窗口常常抓出**全黑**；CPU 拷贝，大屏一次 20~40 ms |

所以 ``auto`` 的规则是：**先 dxgi，失败或黑屏退 wgc，再退 gdi**；每块屏记住
上一次成功的后端，下次直接从它开始，失败了才重新走一遍链条。

**选择方式**（先命中者胜）

1. 环境变量 ``UI_LOCATE_CAPTURE``：``auto`` / ``dxgi`` / ``wgc`` / ``gdi``
2. ``runtime.json`` 里的 ``capture``
3. 默认 ``auto``

点名某个后端时只用它，不回落 —— 用户点名通常就是在排查，悄悄换成别的
后端只会让排查失去意义。失败照实报错，错误里写明试过什么。

**坐标系**

和 ``screen`` 模块的约定完全一致：每块屏的图从该屏的 ``(x, y)`` 开始，
``x/y`` 是虚拟桌面里的屏幕绝对坐标，可以为负。DXGI 的
``DesktopCoordinates`` 与 ``GetMonitorInfo`` 在 Per-Monitor V2 DPI 感知下是同一套
物理像素，靠设备名（``\\\\.\\DISPLAY1``）把两边对上，不靠枚举顺序。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

BACKENDS = ("auto", "dxgi", "wgc", "gdi")
AUTO_CHAIN = ("dxgi", "wgc", "gdi")

# 一张图的最亮像素低于这个值就当「抓黑了」。真实桌面哪怕是深色主题，
# 文字和图标也远高于它；全黑帧（GDI 抓独占全屏）则恒为 0。
BLACK_MAX = 8

# 记住的后端不是链首（dxgi）时，隔这么久重新从链首试一次。一次偶发失败
# （驱动重置、ACCESS_LOST、某一帧抽风）不该让这块屏永远停在慢 / 降级的后端上。
STICKY_RETRY_S = 30.0


class CaptureError(RuntimeError):
    """某个后端抓不到图。信息里写明是哪个后端、为什么。"""


def preferred() -> str:
    """当前要用的后端名。非法值退回 ``auto``。"""
    value = os.environ.get("UI_LOCATE_CAPTURE", "").strip().lower()
    if not value:
        try:
            from ui_locate.config import runtime_settings

            raw = runtime_settings().get("capture")
            value = raw.strip().lower() if isinstance(raw, str) else ""
        except Exception:
            value = ""
    return value if value in BACKENDS else "auto"


def is_black(bgr: np.ndarray) -> bool:
    """整张图是不是近乎全黑。隔点采样，4K 图也只看几万个像素。"""
    if bgr.size == 0:
        return True
    return int(bgr[::7, ::7].max()) < BLACK_MAX


@dataclass
class Region:
    """要抓的一块屏。``device`` 是 ``\\\\.\\DISPLAYn``，坐标是屏幕绝对坐标。"""

    device: str
    x: int
    y: int
    w: int
    h: int


@dataclass
class Shot:
    bgr: np.ndarray
    backend: str
    ms: float
    tried: list[str] = field(default_factory=list)


# ── 各后端的实现 ────────────────────────────────────────────────────────────


def _grab_gdi(region: Region) -> np.ndarray:
    """BitBlt：抓整个虚拟桌面再裁。

    不用 ``bbox=``：实测对负坐标 bbox 会返回全黑（见 ``screen`` 模块说明）。
    """
    import cv2
    from PIL import ImageGrab

    from ui_locate import screen

    full = ImageGrab.grab(all_screens=True)
    vx, vy, vw, vh = screen.virtual_rect()
    if (full.width, full.height) != (vw, vh):
        raise CaptureError(
            f"gdi: 抓屏尺寸 {full.width}x{full.height} 与虚拟桌面 {vw}x{vh} 不一致，"
            "DPI 感知未生效或显示器热插拔"
        )
    x0 = max(0, region.x - vx)
    y0 = max(0, region.y - vy)
    x1 = min(full.width, x0 + region.w)
    y1 = min(full.height, y0 + region.h)
    crop = full.crop((x0, y0, x1, y1))
    return cv2.cvtColor(np.asarray(crop), cv2.COLOR_RGB2BGR)


class _DxPool:
    """dxcam 相机池：每个 (后端, 设备名) 一个相机，建一次反复用。

    建相机要几十到上百毫秒（dxcam 自己还 sleep 0.1s），每次抓屏都建就把
    DXGI 的速度优势全吃掉了。设备丢失（全屏切换、分辨率变化、UAC）时整个
    相机作废重建一次；重建还失败才算这个后端不可用。
    """

    def __init__(self) -> None:
        self._cams: dict[tuple[str, str], object] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _module():
        try:
            import dxcam  # noqa: F401
        except Exception as error:  # ImportError，或 winrt 扩展缺件
            raise CaptureError(
                f"dxcam 不可用（{type(error).__name__}: {error}）。"
                "在设置页重新「准备环境」，或手动 pip install dxcam"
            ) from error
        return dxcam

    def _locate(self, dxcam, device: str) -> tuple[int, int]:
        """按设备名找 (adapter, output) 下标。

        dxcam 只按下标建相机；设备名匹配要走它的工厂对象（模块级 ``__factory``，
        在模块作用域里不会被改名）。枚举顺序不保证与 ``EnumDisplayMonitors``
        一致，所以必须按名字对。
        """
        factory = vars(dxcam).get("__factory")
        outputs = getattr(factory, "outputs", None)
        if outputs is None:
            raise CaptureError("dxcam 版本不兼容：找不到 outputs 列表")
        wanted = device.strip().lower()
        for adapter_idx, outs in enumerate(outputs):
            for output_idx, out in enumerate(outs):
                if str(getattr(out, "devicename", "")).strip().lower() == wanted:
                    return adapter_idx, output_idx
        names = [str(getattr(o, "devicename", "?")) for outs in outputs for o in outs]
        raise CaptureError(f"DXGI 里找不到显示器 {device}（DXGI 看到的是 {names}）")

    def _create(self, backend: str, device: str):
        dxcam = self._module()
        adapter_idx, output_idx = self._locate(dxcam, device)
        return dxcam.create(
            device_idx=adapter_idx,
            output_idx=output_idx,
            output_color="BGR",
            backend="dxgi" if backend == "dxgi" else "winrt",
        )

    def _drop(self, key: tuple[str, str]) -> None:
        cam = self._cams.pop(key, None)
        if cam is not None:
            try:
                cam.release()
            except Exception:
                pass

    def grab(self, backend: str, region: Region, wait_s: float = 0.5) -> np.ndarray:
        key = (backend, region.device.lower())
        with self._lock:
            last_error: Exception | None = None
            for attempt in range(2):  # 第二次是丢掉旧相机后重建
                try:
                    cam = self._cams.get(key)
                    if cam is None:
                        cam = self._create(backend, region.device)
                        self._cams[key] = cam
                    frame = None
                    deadline = time.perf_counter() + wait_s
                    # 刚建好的相机可能还没有第一帧；画面静止时 DXGI 也不会主动
                    # 给新帧。new_frame_only=False 让它在没新帧时返回上一帧。
                    while frame is None and time.perf_counter() < deadline:
                        frame = cam.grab(new_frame_only=False)
                        if frame is None:
                            time.sleep(0.01)
                    if frame is None:
                        raise CaptureError(f"{backend}: {wait_s:.1f}s 内没拿到任何一帧")
                    if frame.shape[1] != region.w or frame.shape[0] != region.h:
                        raise CaptureError(
                            f"{backend}: 帧尺寸 {frame.shape[1]}x{frame.shape[0]} 与显示器 "
                            f"{region.w}x{region.h} 不一致"
                        )
                    return np.ascontiguousarray(frame)
                except CaptureError as error:
                    # 模块缺失、找不到显示器这种重建也没用的，直接报
                    if attempt == 0 and "dxcam 不可用" not in str(error) and "找不到显示器" not in str(error):
                        last_error = error
                        self._drop(key)
                        continue
                    self._drop(key)
                    raise
                except Exception as error:  # 设备丢失等 COM 错误：重建一次
                    last_error = error
                    self._drop(key)
            raise CaptureError(f"{backend}: {type(last_error).__name__}: {last_error}")

    def release(self) -> None:
        with self._lock:
            for key in list(self._cams):
                self._drop(key)


_POOL = _DxPool()


def _grab_dxgi(region: Region) -> np.ndarray:
    return _POOL.grab("dxgi", region)


def _grab_wgc(region: Region) -> np.ndarray:
    return _POOL.grab("wgc", region)


GRABBERS: dict[str, Callable[[Region], np.ndarray]] = {
    "dxgi": _grab_dxgi,
    "wgc": _grab_wgc,
    "gdi": _grab_gdi,
}


# ── 选择与回落 ──────────────────────────────────────────────────────────────


class Capturer:
    """按偏好选后端，auto 时带回落，并记住每块屏上次成功的后端。"""

    def __init__(self, grabbers: dict[str, Callable[[Region], np.ndarray]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.grabbers = grabbers if grabbers is not None else GRABBERS
        self._clock = clock
        self._sticky: dict[str, str] = {}
        self._sticky_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self.last: dict = {}

    def _chain(self, choice: str, device: str) -> list[str]:
        if choice != "auto":
            return [choice]
        key = device.lower()
        sticky = self._sticky.get(key)
        chain = list(AUTO_CHAIN)
        if sticky in chain and sticky != chain[0]:
            since = self._sticky_at.setdefault(key, self._clock())
            if self._clock() - since >= STICKY_RETRY_S:
                # 到期：这一次按原顺序从链首试；首选恢复了就换回去，没恢复
                # 照样落回原来那个，并重新计时
                self._sticky_at[key] = self._clock()
                return chain
        if sticky in chain:
            chain.remove(sticky)
            chain.insert(0, sticky)
        return chain

    def grab(self, region: Region, choice: str | None = None) -> Shot:
        choice = (choice or preferred()).lower()
        if choice not in BACKENDS:
            choice = "auto"
        tried: list[str] = []
        errors: list[str] = []
        black: tuple[str, np.ndarray, float] | None = None
        for name in self._chain(choice, region.device):
            t0 = time.perf_counter()
            try:
                bgr = self.grabbers[name](region)
            except Exception as error:
                tried.append(name)
                errors.append(f"{name}: {error}")
                continue
            ms = (time.perf_counter() - t0) * 1000
            tried.append(name)
            if choice == "auto" and is_black(bgr):
                # 黑帧不一定是错：屏幕可能真的是黑的（游戏过场、关灯的显示器）。
                # 先记下来，后面的后端都失败或也全黑时就用它。
                errors.append(f"{name}: 全黑帧")
                if black is None:
                    black = (name, bgr, ms)
                continue
            with self._lock:
                key = region.device.lower()
                if self._sticky.get(key) != name:
                    self._sticky_at[key] = self._clock()
                self._sticky[key] = name
            shot = Shot(bgr, name, ms, tried)
            self._remember(region, shot, errors)
            return shot
        if black is not None:
            shot = Shot(black[1], black[0], black[2], tried)
            self._remember(region, shot, errors)
            return shot
        raise CaptureError(f"{region.device} 抓屏失败（{choice}）：" + "；".join(errors))

    def _remember(self, region: Region, shot: Shot, errors: list[str]) -> None:
        self.last = {
            "device": region.device,
            "backend": shot.backend,
            "ms": round(shot.ms, 1),
            "tried": list(shot.tried),
            "notes": list(errors),
        }

    def status(self) -> dict:
        return {
            "preferred": preferred(),
            "sticky": dict(self._sticky),
            "last": dict(self.last),
        }


CAPTURER = Capturer()


def release() -> None:
    """释放所有 DXGI / WGC 相机（服务退出时调用）。"""
    _POOL.release()
