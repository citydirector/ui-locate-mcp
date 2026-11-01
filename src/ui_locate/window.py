"""按窗口工作：找窗口、把它切到前台、只抓它自己（被挡住也能抓）。

**为什么要有这一层**

1. **前台**。SendInput 走系统输入队列，事件只会送给*当前前台*窗口。模型通常
   是在 DSH / 浏览器 / 终端里调本工具的，那一刻前台是它们，不是游戏。不先把
   游戏切到前台，按键就打进了聊天框；点击的第一下常被游戏当成「激活窗口」
   吃掉。而 ``SetForegroundWindow`` 在「前台属于别的进程」时会被系统**静默
   忽略**（前台锁），所以要按顺序试几种办法，并回读确认。

2. **被挡住也能抓**。按屏抓（DXGI / WGC / GDI）抓到的是屏幕上看得见的东西：
   游戏被 DSH 盖住时抓到的就是 DSH。``PrintWindow(PW_RENDERFULLCONTENT)``
   让窗口把自己画到我们的位图上，被遮挡的部分也在，而且只有客户区、没有
   标题栏。代价：独占全屏的 D3D 程序、部分 Vulkan 程序会画出全黑 —— 这时
   回落到「按屏抓再裁出窗口那块」（要求窗口在屏幕上可见）。

**写法**：其它工具的 ``monitor`` 参数可以写成

  - ``"window:原神"``      按标题找（先全等，再忽略大小写的包含）
  - ``"hwnd:132456"``      按句柄（十进制或 0x 十六进制）

得到的图左上角 = 窗口客户区左上角的**屏幕绝对坐标**，和按屏抓的约定一致，
所以定位、点击、验证都不用改。

**抓窗方式**（先命中者胜）：环境变量 ``UI_LOCATE_WINDOW_CAPTURE`` /
``runtime.json`` 的 ``"window_capture"``：``auto``（默认：PrintWindow，黑了再按屏
裁）/ ``printwindow`` / ``screen``。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

import numpy as np

_IS_WINDOWS = sys.platform == "win32"

WINDOW_CAPTURE = ("auto", "printwindow", "screen")

# 这些是桌面、任务栏这类「壳」窗口：点到它们时不该去抢前台。
_SHELL_CLASSES = {"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd"}

LAST: dict = {}


class WindowError(RuntimeError):
    """找不到窗口 / 抓不到窗口。"""


@dataclass
class WinInfo:
    hwnd: int
    title: str
    cls: str
    pid: int
    rect: tuple[int, int, int, int]      # 客户区，屏幕绝对坐标 (x, y, w, h)
    foreground: bool = False
    minimized: bool = False

    def as_dict(self) -> dict:
        x, y, w, h = self.rect
        return {"hwnd": self.hwnd, "title": self.title, "class": self.cls, "pid": self.pid,
                "x": x, "y": y, "w": w, "h": h,
                "foreground": self.foreground, "minimized": self.minimized,
                "monitor": f"hwnd:{self.hwnd}"}


# ── 纯逻辑（任何平台都能测）─────────────────────────────────────────────────

def parse_spec(spec: object) -> dict | None:
    """``"window:标题"`` → ``{"title": ...}``；``"hwnd:123"`` → ``{"hwnd": 123}``；其它 → None。"""
    if isinstance(spec, dict):
        if spec.get("hwnd"):
            return {"hwnd": int(spec["hwnd"])}
        if spec.get("title"):
            return {"title": str(spec["title"])}
        return None
    if not isinstance(spec, str):
        return None
    s = spec.strip()
    low = s.lower()
    if low.startswith("hwnd:"):
        raw = s[5:].strip()
        try:
            return {"hwnd": int(raw, 16) if raw.lower().startswith("0x") else int(raw)}
        except ValueError:
            raise WindowError(f"hwnd 写法不对：{spec!r}（要十进制或 0x 十六进制）") from None
    if low.startswith("window:"):
        title = s[7:].strip()
        if not title:
            raise WindowError("window: 后面要写窗口标题（或其中一段）")
        return {"title": title}
    return None


def is_window_spec(spec: object) -> bool:
    try:
        return parse_spec(spec) is not None
    except WindowError:
        return True


def as_spec(window: object) -> str:
    """MCP 里的 ``window`` 参数 → ``monitor`` 写法。数字当句柄，其它当标题。"""
    if window in (None, "", 0):
        return ""
    if isinstance(window, int):
        return f"hwnd:{window}"
    s = str(window).strip()
    if is_window_spec(s):
        return s
    if s.isdecimal() or s.lower().startswith("0x"):
        return f"hwnd:{s}"
    return f"window:{s}"


def pick(windows: list[WinInfo], query: dict) -> WinInfo | None:
    """在窗口列表里挑一个。标题全等 > 前缀 > 包含；同档里前台优先、面积大的优先。"""
    if query.get("hwnd"):
        return next((w for w in windows if w.hwnd == int(query["hwnd"])), None)
    q = str(query.get("title") or "").strip().lower()
    if not q:
        return None

    def tier(w: WinInfo) -> int:
        t = w.title.strip().lower()
        if t == q:
            return 0
        if t.startswith(q):
            return 1
        if q in t:
            return 2
        return 9

    cands = [w for w in windows if tier(w) < 9]
    if not cands:
        return None
    cands.sort(key=lambda w: (tier(w), not w.foreground, w.minimized, -(w.rect[2] * w.rect[3])))
    return cands[0]


def preferred_capture() -> str:
    value = os.environ.get("UI_LOCATE_WINDOW_CAPTURE", "").strip().lower()
    if not value:
        try:
            from ui_locate.config import runtime_settings

            raw = runtime_settings().get("window_capture")
            value = raw.strip().lower() if isinstance(raw, str) else ""
        except Exception:
            value = ""
    return value if value in WINDOW_CAPTURE else "auto"


# ── Win32 ──────────────────────────────────────────────────────────────────

if _IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wt

    _u = ctypes.WinDLL("user32", use_last_error=True)
    _g = ctypes.WinDLL("gdi32", use_last_error=True)
    _k = ctypes.WinDLL("kernel32", use_last_error=True)

    _EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    _u.EnumWindows.argtypes = [_EnumProc, wt.LPARAM]
    _u.GetWindowTextLengthW.argtypes = [wt.HWND]
    _u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    _u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    _u.IsWindowVisible.argtypes = [wt.HWND]
    _u.IsWindow.argtypes = [wt.HWND]
    _u.IsIconic.argtypes = [wt.HWND]
    _u.GetForegroundWindow.restype = wt.HWND
    _u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _u.GetWindowThreadProcessId.restype = wt.DWORD
    _u.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    _u.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    _u.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    _u.GetAncestor.restype = wt.HWND
    _u.WindowFromPoint.argtypes = [wt.POINT]
    _u.WindowFromPoint.restype = wt.HWND
    _u.SetForegroundWindow.argtypes = [wt.HWND]
    _u.BringWindowToTop.argtypes = [wt.HWND]
    _u.SetActiveWindow.argtypes = [wt.HWND]
    _u.SetFocus.argtypes = [wt.HWND]
    _u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    _u.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    _u.GetWindowDC.argtypes = [wt.HWND]
    _u.GetWindowDC.restype = wt.HDC
    _u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    _u.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    _u.PrintWindow.restype = wt.BOOL
    _u.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]
    _g.CreateCompatibleDC.argtypes = [wt.HDC]
    _g.CreateCompatibleDC.restype = wt.HDC
    _g.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    _g.CreateCompatibleBitmap.restype = wt.HBITMAP
    _g.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    _g.SelectObject.restype = wt.HGDIOBJ
    _g.DeleteObject.argtypes = [wt.HGDIOBJ]
    _g.DeleteDC.argtypes = [wt.HDC]
    _k.GetCurrentThreadId.restype = wt.DWORD

    class _BMIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    _g.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
                             ctypes.POINTER(_BMIH), wt.UINT]

PW_CLIENTONLY = 0x1
PW_RENDERFULLCONTENT = 0x2
GA_ROOT = 2
SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x2


def _require() -> None:
    if not _IS_WINDOWS:
        raise WindowError(f"按窗口操作只支持 Windows，当前是 {sys.platform}")


def _text(hwnd: int) -> str:
    n = _u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _u.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid_tid(hwnd: int) -> tuple[int, int]:
    pid = wt.DWORD(0)
    tid = _u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value), int(tid)


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """客户区的屏幕绝对坐标 (x, y, w, h)。服务进程是 Per-Monitor V2，所以是物理像素。"""
    _require()
    r = wt.RECT()
    if not _u.GetClientRect(hwnd, ctypes.byref(r)):
        raise WindowError(f"窗口 {hwnd} 已不存在")
    pt = wt.POINT(0, 0)
    _u.ClientToScreen(hwnd, ctypes.byref(pt))
    return int(pt.x), int(pt.y), int(r.right - r.left), int(r.bottom - r.top)


def _info(hwnd: int, fg: int) -> WinInfo:
    pid, _ = _pid_tid(hwnd)
    try:
        rect = client_rect(hwnd)
    except WindowError:
        rect = (0, 0, 0, 0)
    return WinInfo(hwnd, _text(hwnd), _class(hwnd), pid, rect,
                   foreground=hwnd == fg, minimized=bool(_u.IsIconic(hwnd)))


def list_windows(title: str = "") -> list[WinInfo]:
    """可见的、有标题的顶层窗口。``title`` 给了就只留标题里包含它的。"""
    _require()
    from ui_locate import screen

    screen.set_dpi_aware()
    fg = int(_u.GetForegroundWindow() or 0)
    found: list[int] = []

    def cb(hwnd, _lp):
        if _u.IsWindowVisible(hwnd) and _u.GetWindowTextLengthW(hwnd) > 0:
            found.append(int(hwnd))
        return True

    _u.EnumWindows(_EnumProc(cb), 0)
    out = []
    q = title.strip().lower()
    for h in found:
        info = _info(h, fg)
        if info.cls.lower() in _SHELL_CLASSES:
            continue
        if q and q not in info.title.lower():
            continue
        out.append(info)
    return out


def resolve(spec: object) -> WinInfo:
    """``window:`` / ``hwnd:`` 写法 → 一个窗口。找不到就抛，错误里列出相近的候选。"""
    _require()
    query = parse_spec(spec)
    if query is None:
        raise WindowError(f"不是窗口写法：{spec!r}")
    if query.get("hwnd"):
        h = int(query["hwnd"])
        if not _u.IsWindow(h):
            raise WindowError(f"句柄 {h} 对应的窗口已不存在")
        return _info(h, int(_u.GetForegroundWindow() or 0))
    wins = list_windows()
    hit = pick(wins, query)
    if hit is None:
        names = ", ".join(repr(w.title) for w in wins[:12])
        raise WindowError(f"没有标题含 {query['title']!r} 的窗口。现有：{names}")
    return hit


def root_at(x: int, y: int) -> int:
    """屏幕某点下的顶层窗口句柄（子控件往上找到根）。"""
    _require()
    h = int(_u.WindowFromPoint(wt.POINT(int(x), int(y))) or 0)
    if not h:
        return 0
    return int(_u.GetAncestor(h, GA_ROOT) or h)


def foreground() -> int:
    _require()
    return int(_u.GetForegroundWindow() or 0)


def is_shell(hwnd: int) -> bool:
    return bool(hwnd) and _class(hwnd).lower() in _SHELL_CLASSES


# ── 前台 ───────────────────────────────────────────────────────────────────

def _is_front(hwnd: int) -> bool:
    fg = int(_u.GetForegroundWindow() or 0)
    return fg == hwnd or (fg and int(_u.GetAncestor(fg, GA_ROOT) or 0) == hwnd)


def focus(hwnd: int, *, settle_ms: int = 60, tries: int = 2) -> dict:
    """把窗口切到前台，并回读确认。依次试：

    1. 直接 ``SetForegroundWindow``（本进程恰好有前台权时就够了）
    2. 先送一下 Alt 再设：系统把「刚收到输入的进程」视为可以抢前台
    3. ``AttachThreadInput`` 把本线程挂到当前前台线程上再设 —— 前台锁最常见的解法

    最小化的先还原。返回 ``{ok, method, before, after, ms}``；都失败时 ok=false，
    不抛：调用方仍然可以照常发输入，只是要知道很可能打偏了。
    """
    _require()
    t0 = time.perf_counter()
    before = int(_u.GetForegroundWindow() or 0)
    out = {"hwnd": hwnd, "before": before, "title": _text(hwnd)}
    if _is_front(hwnd):
        out.update(ok=True, method="already", after=before,
                   ms=round((time.perf_counter() - t0) * 1000, 1))
        return out
    if _u.IsIconic(hwnd):
        _u.ShowWindow(hwnd, SW_RESTORE)

    def settle() -> bool:
        time.sleep(max(settle_ms, 0) / 1000.0)
        return _is_front(hwnd)

    def plain() -> None:
        _u.BringWindowToTop(hwnd)
        _u.SetForegroundWindow(hwnd)

    def alt() -> None:
        _u.keybd_event(VK_MENU, 0, 0, 0)
        _u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        plain()

    def attach() -> None:
        fg = int(_u.GetForegroundWindow() or 0)
        me = int(_k.GetCurrentThreadId())
        _, fg_tid = _pid_tid(fg) if fg else (0, 0)
        _, tgt_tid = _pid_tid(hwnd)
        linked = []
        for tid in {fg_tid, tgt_tid}:
            if tid and tid != me and _u.AttachThreadInput(me, tid, True):
                linked.append(tid)
        try:
            plain()
            _u.SetActiveWindow(hwnd)
            _u.SetFocus(hwnd)
        finally:
            for tid in linked:
                _u.AttachThreadInput(me, tid, False)

    method = None
    for _ in range(max(tries, 1)):
        for name, fn in (("set_foreground", plain), ("alt", alt), ("attach_thread", attach)):
            try:
                fn()
            except Exception:  # noqa: BLE001 - 某一招失败就换下一招
                continue
            if settle():
                method = name
                break
        if method:
            break
    after = int(_u.GetForegroundWindow() or 0)
    out.update(ok=method is not None, method=method or "failed", after=after,
               ms=round((time.perf_counter() - t0) * 1000, 1))
    if method is None:
        out["hint"] = ("系统拒绝切前台（前台锁 / 目标以管理员运行而本服务不是 / "
                       "全屏独占程序）。输入可能会打到别的窗口。")
    return out


# ── 抓窗口 ─────────────────────────────────────────────────────────────────

def print_window(hwnd: int) -> np.ndarray:
    """PrintWindow 抓客户区，返回 BGR。被遮挡也能抓；独占全屏 D3D 常画出全黑。"""
    _require()
    x, y, w, h = client_rect(hwnd)
    if w <= 0 or h <= 0:
        raise WindowError(f"窗口 {hwnd} 客户区为空（最小化了？）")
    hdc = _u.GetWindowDC(hwnd)
    mdc = _g.CreateCompatibleDC(hdc)
    bmp = _g.CreateCompatibleBitmap(hdc, w, h)
    old = _g.SelectObject(mdc, bmp)
    try:
        ok = _u.PrintWindow(hwnd, mdc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        if not ok:
            raise WindowError(f"PrintWindow 失败（last_error={ctypes.get_last_error()}）")
        bmi = _BMIH()
        bmi.biSize = ctypes.sizeof(_BMIH)
        bmi.biWidth, bmi.biHeight = w, -h          # 负高 = 自上而下
        bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
        buf = np.empty((h, w, 4), dtype=np.uint8)
        got = _g.GetDIBits(mdc, bmp, 0, h, buf.ctypes.data, ctypes.byref(bmi), 0)
        if got != h:
            raise WindowError(f"GetDIBits 只拷到 {got}/{h} 行")
        return np.ascontiguousarray(buf[:, :, :3])
    finally:
        _g.SelectObject(mdc, old)
        _g.DeleteObject(bmp)
        _g.DeleteDC(mdc)
        _u.ReleaseDC(hwnd, hdc)


def _screen_crop(rect: tuple[int, int, int, int]) -> np.ndarray:
    """按屏抓（DXGI / WGC / GDI，按设置），再裁出窗口那块。窗口须可见、没被挡。"""
    from ui_locate import screen

    full, (vx, vy) = screen.grab("all")
    x, y, w, h = rect
    x0, y0 = max(x - vx, 0), max(y - vy, 0)
    x1, y1 = min(x - vx + w, full.shape[1]), min(y - vy + h, full.shape[0])
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise WindowError(f"窗口 {rect} 不在任何显示器上")
    crop = np.ascontiguousarray(full[y0:y1, x0:x1])
    if crop.shape[:2] != (h, w):
        # 窗口有一部分在屏幕外：补黑，保证 原点 + 图内坐标 = 屏幕坐标
        pad = np.zeros((h, w, 3), dtype=np.uint8)
        oy, ox = y0 - (y - vy), x0 - (x - vx)
        pad[oy:oy + crop.shape[0], ox:ox + crop.shape[1]] = crop
        crop = pad
    return crop


def grab(spec: object, method: str | None = None) -> tuple[np.ndarray, tuple[int, int]]:
    """抓一个窗口的客户区，返回 (BGR, 客户区左上角的屏幕坐标)。"""
    from ui_locate.capture import is_black

    win = resolve(spec)
    if win.minimized:
        raise WindowError(f"窗口 {win.title!r} 最小化了，抓不到内容（先 focus 或还原）")
    method = (method or preferred_capture()).lower()
    notes: list[str] = []
    t0 = time.perf_counter()
    bgr = None
    used = None
    if method in ("auto", "printwindow"):
        try:
            img = print_window(win.hwnd)
            if method == "printwindow" or not is_black(img):
                bgr, used = img, "printwindow"
            else:
                notes.append("printwindow: 全黑帧（独占全屏 / 硬件加速常见），改按屏裁")
        except Exception as error:  # noqa: BLE001
            if method == "printwindow":
                raise
            notes.append(f"printwindow: {error}")
    if bgr is None:
        bgr, used = _screen_crop(win.rect), "screen"
    LAST.clear()
    LAST.update({"backend": f"window/{used}", "hwnd": win.hwnd, "title": win.title,
                 "ms": round((time.perf_counter() - t0) * 1000, 1), "notes": notes})
    return bgr, (win.rect[0], win.rect[1])
