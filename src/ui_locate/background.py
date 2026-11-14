"""后台投递：以 hwnd 为目标，UIA 优先、窗口消息兜底，全程不激活窗口。

为什么不按屏幕坐标"碰运气"
--------------------------
``WindowFromPoint`` 只看最上面那层窗口：遮挡、透明覆盖层、输入法候选框都会
让坐标落到别的窗口上。后台投递要的是"这个窗口里的这个控件"，那就必须拿到
hwnd —— 于是本模块**要求 hwnd**（或一个能解析成 hwnd 的窗口写法）。只给
屏幕坐标时明确报错，让调用方先 `ui_locate_windows` 取一次，而不是静默猜一个。

通道
----
1. **UIA**：``ElementFromHandle(叶子 hwnd)`` → ``InvokePattern``（按钮这类）。
   注意这里没有用 ``ElementFromPoint``：本机实测它对任何点都返回
   ``E_ACCESSDENIED``，而 ``ElementFromHandle`` 正常。所以坐标命中交给 Win32
   几何下探，UIA 只负责"按一下"。
2. **窗口消息**：``PostMessage`` 一串 WM_MOUSEMOVE/DOWN/UP（带客户区坐标）。

全程不调用 ``SetForegroundWindow`` / ``SetCursorPos`` / ``SendInput``。

几何下探
--------
``ChildWindowFromPointEx`` 逐层往下（最多 8 层）。每一层都**重新构造一个
POINT 副本**再 ``ScreenToClient``：复用同一个 POINT 会让第二层拿到上一层
已经转换过的坐标，点击就静默偏掉 —— 那种 bug 在截图上看不出来，只会表现为
"点到了旁边"。每一层还要用 ``GetWindowThreadProcessId`` 核对属于目标进程，
避免顺着系统子窗口跑到别的进程里。

白名单
------
消息通道只对已知会老实处理 posted mouse message 的类放行：``#32770`` 对话框
（含 ``Button`` / ``Edit`` / ``Static`` / ``ComboBox`` / ``msctls_*`` 等标准
控件）以及 WinForms（``WindowsForms10.*``）。Chromium / WinUI3 / UWP / 游戏 /
自绘界面一律 ``BACKGROUND_UNAVAILABLE``，**绝不回落前台**：那些框架把
posted 消息当噪声，回落前台则等于抢用户的焦点。

``dispatched`` 只表示"消息进了队列 / Invoke 返回了 S_OK"，不代表界面认账。
是否真的生效由 ``/act`` 的截图证据回答（见 ``act`` 的 ``verified``）。
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes
from typing import Any

from . import uia
from .actions import BackgroundUnavailable, InputError, Paused

# 单独一层，测试里可以换掉（否则每条 WM_ACTIVATE 都会真睡 10ms）。
_sleep = time.sleep

DEFAULT_BUDGET_MS = 800
MAX_DEPTH = 8

WM_ACTIVATE = 0x0006
WA_ACTIVE = 1
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010

CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPDISABLED = 0x0002

ALLOWED_ROOT_CLASSES = frozenset({"#32770"})
ALLOWED_ROOT_PREFIXES = ("WindowsForms10.",)
ALLOWED_CONTROL_CLASSES = frozenset({
    "Button", "Edit", "Static", "ComboBox",
    "SysListView32", "SysTreeView32", "ToolbarWindow32", "ReBarWindow32",
})
ALLOWED_CONTROL_PREFIXES = ("msctls_", "WindowsForms10.")

# 这些框架把 posted 消息当噪声：控件是自绘的，消息进不到"按钮"那一层。
DENIED_PREFIXES = (
    "Chrome_WidgetWin",
    "Chrome_RenderWidgetHostHWND",
    "ApplicationFrameWindow",
    "Windows.UI.Core.CoreWindow",
    "Microsoft.UI.Content",
    "MSCTFIME",
    "Intermediate D3D Window",
)

# 发送前要不要先让目标"认为自己被激活"：
#   never  —— 一条激活消息都不发；
#   auto   —— 目标当前不是前台才发（默认）；
#   always —— 一律发。
# 风险：极少数应用收到 WM_ACTIVATE 会自己调前台 API。它不改变真实前台，
# 但对方可以自己去改 —— 所以默认不是 always。
ACTIVATE_MODES = ("never", "auto", "always")

BUTTON_FLAGS = {
    "left": (MK_LBUTTON, WM_LBUTTONDOWN, WM_LBUTTONUP),
    "right": (MK_RBUTTON, WM_RBUTTONDOWN, WM_RBUTTONUP),
    "middle": (MK_MBUTTON, WM_MBUTTONDOWN, WM_MBUTTONUP),
}

# UIA 明确"什么都没发生"的结局：这些才可以转消息通道。
# timeout / busy / error 都**不知道**动作是否已经执行，转投等于可能点两下。
_UIA_DEFINITIVE_MISSES = frozenset({"no_pattern", "no_element", "unavailable"})

_WINDLL = getattr(ctypes, "WinDLL", None)
_user32 = _WINDLL("user32", use_last_error=True) if _WINDLL is not None else None
_kernel32 = _WINDLL("kernel32", use_last_error=True) if _WINDLL is not None else None


# ── 暂停开关 ────────────────────────────────────────────────────────────────
#
# 参考实现用全局热键 Ctrl+Alt+Q。热键要装钩子、要处理"钩子被别的程序吃掉"，
# 这一轮先不做（TODO），只提供进程内状态 + HTTP / MCP 两个入口。不落盘时
# 状态随进程消失 —— 对"我现在不想让它动我的桌面"这个诉求来说，够用且可预期。

_pause_lock = threading.Lock()
_paused = False
_paused_reason: str | None = None
_paused_since: float | None = None

PAUSE_ENV = "UI_LOCATE_PAUSE_FILE"


def state() -> dict:
    with _pause_lock:
        return {"paused": _paused, "reason": _paused_reason,
                "since": _paused_since,
                "file": os.environ.get(PAUSE_ENV) or None}


def paused() -> bool:
    with _pause_lock:
        return _paused


def _persist(payload: dict) -> None:
    path = os.environ.get(PAUSE_ENV)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    except OSError:
        # 落盘失败不该让"暂停"失效：内存里的状态仍然是权威。
        pass


def pause(reason: str | None = None) -> dict:
    global _paused, _paused_reason, _paused_since
    with _pause_lock:
        _paused = True
        _paused_reason = reason or None
        _paused_since = time.time()
        out = {"paused": True, "reason": _paused_reason, "since": _paused_since}
    _persist(out)
    return out


def resume() -> dict:
    global _paused, _paused_reason, _paused_since
    with _pause_lock:
        was = _paused
        _paused = False
        _paused_reason = None
        _paused_since = None
    _persist({"paused": False})
    return {"paused": False, "was_paused": was}


def require_running() -> None:
    """暂停中一律拒绝，并且是一个**明确**的错误（不是悄悄不做事）。"""
    if paused():
        st = state()
        raise Paused("ui-locate 已暂停：所有输入动作都被拒绝",
                     reason=st.get("reason") or "",
                     since=st.get("since"))


# ── Win32 薄封装 ────────────────────────────────────────────────────────────


def _require() -> None:
    if _user32 is None:
        raise BackgroundUnavailable("后台投递需要 Windows（user32 不可用）",
                                    reason="not_windows")


def _alive(hwnd: int) -> bool:
    return bool(_user32.IsWindow(wintypes.HWND(hwnd)))


def _visible(hwnd: int) -> bool:
    return bool(_user32.IsWindowVisible(wintypes.HWND(hwnd)))


def _minimized(hwnd: int) -> bool:
    return bool(_user32.IsIconic(wintypes.HWND(hwnd)))


def _hung(hwnd: int) -> bool:
    try:
        return bool(_user32.IsHungAppWindow(wintypes.HWND(hwnd)))
    except AttributeError:  # 老系统上没有这个导出
        return False


def _cls(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def _title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(wintypes.HWND(hwnd), buf, 512)
    return buf.value


def _pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if not _user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    top_left = wintypes.POINT(0, 0)
    if not _user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(top_left)):
        return None
    return (int(top_left.x), int(top_left.y),
            int(rect.right - rect.left), int(rect.bottom - rect.top))


def _screen_to_client(hwnd: int, x: float, y: float) -> tuple[int, int] | None:
    """屏幕坐标 → 客户区坐标。每次都新建 POINT，绝不复用。"""
    pt = wintypes.POINT(int(round(x)), int(round(y)))
    if not _user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(pt)):
        return None
    return (int(pt.x), int(pt.y))


def _child_at(hwnd: int, x: float, y: float) -> int:
    """屏幕坐标下的直接子窗口（不可见/禁用的跳过）。每次新建 POINT。"""
    pt = wintypes.POINT(int(round(x)), int(round(y)))
    if not _user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(pt)):
        return 0
    child = _user32.ChildWindowFromPointEx(
        wintypes.HWND(hwnd), pt, CWP_SKIPINVISIBLE | CWP_SKIPDISABLED)
    return int(child or 0)


def _foreground() -> int:
    return int(_user32.GetForegroundWindow() or 0)


def _root_owner(hwnd: int) -> int:
    """GA_ROOTOWNER：顶层"属主"窗口（模态弹窗挂在它下面）。"""
    return int(_user32.GetAncestor(wintypes.HWND(hwnd), 3) or 0)


def _last_active_popup(root: int) -> int:
    if not root:
        return 0
    return int(_user32.GetLastActivePopup(wintypes.HWND(root)) or 0)


def _post(hwnd: int, msg: int, wparam: int, lparam: int) -> bool:
    return bool(_user32.PostMessageW(wintypes.HWND(hwnd), int(msg),
                                     wintypes.WPARAM(wparam), wintypes.LPARAM(lparam)))


def _make_lparam(x: int, y: int) -> int:
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def _key_lparam(scan: int, *, up: bool = False) -> int:
    """WM_KEYDOWN / WM_KEYUP 的 lParam（MAA ``make_key*_lparam`` 同一套位）。

    bit 16-23 = 扫描码，bit 30 = 之前是按下状态，bit 31 = 正在抬起。很多程序
    靠这两位区分"自动重复"和"真的松开"；只填扫描码会让它们把 keyup 当成
    又一次按下。
    """
    value = 1 | ((int(scan) & 0xFF) << 16)
    if up:
        value |= (1 << 30) | (1 << 31)
    return value


class _Budget:
    """每个 Win32 调用之间的预算检查。超了就明确失败，不无限拖。"""

    def __init__(self, ms: int) -> None:
        self.ms = max(50, int(ms))
        self.deadline = time.monotonic() + self.ms / 1000.0
        self.steps: list[str] = []

    def mark(self, name: str) -> None:
        self.steps.append(name)
        if time.monotonic() > self.deadline:
            raise BackgroundUnavailable(
                f"后台投递超过 {self.ms}ms 预算（停在 {name}）",
                reason="budget_exhausted")

    def left(self) -> int:
        return max(0, int((self.deadline - time.monotonic()) * 1000))


# ── 目标解析与校验 ──────────────────────────────────────────────────────────


def _win_module() -> Any:
    from . import window as window_module
    return window_module


def resolve(*, hwnd: int = 0, window: Any = None, win: Any = None) -> dict:
    """把 hwnd / 窗口写法解析成一个目标描述。拿不到就是明确错误。"""
    _require()
    target_hwnd = int(hwnd or 0)
    if not target_hwnd and window not in (None, "", 0):
        module = win if win is not None else _win_module()
        try:
            info = module.resolve(window)
        except Exception as exc:  # noqa: BLE001 - 窗口找不到就是找不到
            raise BackgroundUnavailable(f"找不到窗口 {window!r}：{exc}",
                                        reason="window_not_found") from exc
        target_hwnd = int(getattr(info, "hwnd", 0) or 0)
    if not target_hwnd:
        raise BackgroundUnavailable(
            "后台投递需要 hwnd：请先取窗口（ui_locate_windows 或 window 参数），"
            "只有屏幕坐标时不会去猜目标窗口。",
            reason="hwnd_required")
    if not _alive(target_hwnd):
        raise BackgroundUnavailable(f"窗口 {target_hwnd:#x} 已经不存在",
                                    reason="window_gone")
    rect = _client_rect(target_hwnd)
    return {
        "hwnd": target_hwnd,
        "pid": _pid(target_hwnd),
        "cls": _cls(target_hwnd),
        "title": _title(target_hwnd),
        "client": rect,
        "minimized": _minimized(target_hwnd),
        "visible": _visible(target_hwnd),
    }


def _denied(cls: str) -> str | None:
    for prefix in DENIED_PREFIXES:
        if cls.startswith(prefix):
            return (f"{cls} 属于自绘/合成框架（Chromium、WinUI3、UWP 一类），"
                    "posted 消息不会被当成用户输入")
    return None


def _allowed_control(cls: str) -> bool:
    return cls in ALLOWED_CONTROL_CLASSES or cls.startswith(ALLOWED_CONTROL_PREFIXES)


def _allowed_root(cls: str) -> bool:
    return cls in ALLOWED_ROOT_CLASSES or cls.startswith(ALLOWED_ROOT_PREFIXES)


def check_target(target: dict, *, budget: _Budget | None = None) -> None:
    """目标窗口是否允许后台投递。不满足就抛（绝不返回"试试前台"）。"""
    budget = budget or _Budget(DEFAULT_BUDGET_MS)
    budget.mark("check_target")
    hwnd = target["hwnd"]
    if not target.get("visible"):
        raise BackgroundUnavailable(
            f"窗口 {hwnd:#x} 不可见：后台投递要求窗口在屏幕上（最小化/隐藏的窗口"
            "不会渲染，也没有可靠的点击语义）", reason="not_visible")
    if target.get("minimized"):
        raise BackgroundUnavailable(f"窗口 {hwnd:#x} 已最小化，后台投递不适用",
                                    reason="minimized")
    if _hung(hwnd):
        raise BackgroundUnavailable(f"窗口 {hwnd:#x} 无响应（IsHungAppWindow）",
                                    reason="hung")
    module = _win_module()
    try:
        if module.is_shell(hwnd):
            raise BackgroundUnavailable(f"{target.get('cls')} 是系统外壳窗口",
                                        reason="shell_window")
    except BackgroundUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 外壳判定失败不影响主流程
        pass
    denied = _denied(str(target.get("cls") or ""))
    if denied:
        raise BackgroundUnavailable(denied, reason="class_denied")
    confidence = module.uipi(hwnd) if hasattr(module, "uipi") else None
    if isinstance(confidence, dict) and confidence.get("blocked"):
        raise BackgroundUnavailable(
            "目标进程的完整性级别高于本进程（UIPI）：消息会被系统静默丢弃",
            reason="uipi_blocked")


def descend(target: dict, x: float, y: float, *, budget: _Budget | None = None) -> dict:
    """纯几何下探：屏幕坐标 → 最深的、属于同一进程的子窗口。"""
    budget = budget or _Budget(DEFAULT_BUDGET_MS)
    hwnd = target["hwnd"]
    pid = target["pid"]
    chain = [{"hwnd": hwnd, "cls": target.get("cls") or _cls(hwnd), "pid": pid}]
    current = hwnd
    for _ in range(MAX_DEPTH):
        budget.mark(f"descend:{len(chain)}")
        child = _child_at(current, x, y)
        if not child or child == current:
            break
        child_pid = _pid(child)
        if child_pid != pid:
            # 跑到别的进程去了（宿主控件、系统子窗口）：停在上一个安全的层。
            break
        current = child
        chain.append({"hwnd": current, "cls": _cls(current), "pid": child_pid})
    return {"chain": chain, "leaf": chain[-1], "depth": len(chain) - 1}


# ── 投递 ────────────────────────────────────────────────────────────────────


def _click_messages(hwnd: int, cx: int, cy: int, *, button: str, count: int,
                    delay_ms: int) -> dict:
    """按真实鼠标的形状投递：悬停 → 按下 → **按住移动** → 抬起。

    中间那条「按住状态下的移动」不是多余的：不少控件（WinForms 的 Button、
    部分 msctls_*）要看到 ``WM_MOUSEMOVE`` 且 ``wParam`` 带键位，才把"指针确实
    在这块控件上、键确实按着"这两件事都建立起来；少了它，down/up 可能被
    静默丢掉 —— 表现就是 ``dispatched: true`` 而界面毫无变化。
    """
    mk, down, up = BUTTON_FLAGS.get(button, BUTTON_FLAGS["left"])
    lparam = _make_lparam(cx, cy)
    posted: list[str] = []
    for i in range(max(1, int(count))):
        if i:
            time.sleep(max(0, delay_ms) / 1000.0)
        if not _post(hwnd, WM_MOUSEMOVE, 0, lparam):
            return {"sent": False, "posted": posted, "reason": "WM_MOUSEMOVE 被拒绝"}
        posted.append("move")
        if not _post(hwnd, down, mk, lparam):
            return {"sent": False, "posted": posted, "reason": "WM_*BUTTONDOWN 被拒绝"}
        posted.append("down")
        if not _post(hwnd, WM_MOUSEMOVE, mk, lparam):
            return {"sent": False, "posted": posted, "reason": "WM_MOUSEMOVE(mk) 被拒绝"}
        posted.append("move_pressed")
        if not _post(hwnd, up, 0, lparam):
            return {"sent": False, "posted": posted, "reason": "WM_*BUTTONUP 被拒绝"}
        posted.append("up")
    return {"sent": True, "posted": posted, "client": [cx, cy]}


def _activate_mode(activate: str | None) -> str:
    mode = str(activate or "auto").strip().lower()
    if mode not in ACTIVATE_MODES:
        raise BackgroundUnavailable(
            f"不认识的 activate {activate!r}；可用 never / auto / always",
            reason="bad_activate")
    return mode


def _activate(hwnd: int, mode: str) -> dict:
    """按需发一条 WM_ACTIVATE，让控件进入"窗口是活的"状态。

    不发 SetForegroundWindow —— 真实前台一点不动。风险在于极少数应用收到
    这条消息后自己会去抢前台，所以默认只在目标本来就不是前台时才发。
    """
    if mode == "never":
        return {"sent": False, "skipped": "never"}
    if mode == "auto":
        fg = _foreground()
        if fg and (fg == hwnd or _root_owner(fg) == _root_owner(hwnd)):
            return {"sent": False, "skipped": "already_foreground"}
    if not _post(hwnd, WM_ACTIVATE, WA_ACTIVE, 0):
        return {"sent": False, "skipped": "post_failed"}
    _sleep(0.010)
    return {"sent": True, "mode": mode}


def _active_popup_target(target: dict, x: float, y: float, *,
                         budget: _Budget | None = None,
                         win: Any = None) -> dict:
    """模态弹窗出现时，点击真正该去的是那个弹窗，不是传入的 hwnd。

    做法同 MAA ``get_active_hwnd``：``GA_ROOTOWNER`` → ``GetLastActivePopup``。
    弹窗不合法（不可见 / 已消失 / 过不了检查）就老老实实用原目标，不猜。
    """
    hwnd = int(target["hwnd"])
    popup = _last_active_popup(_root_owner(hwnd))
    if not popup or popup == hwnd or not _visible(popup):
        return target
    candidate = {
        "hwnd": popup, "pid": _pid(popup), "cls": _cls(popup), "title": _title(popup),
        "client": _client_rect(popup), "minimized": _minimized(popup),
        "visible": _visible(popup),
    }
    try:
        check_target(candidate, budget=budget)
    except InputError:
        return target
    rect = candidate["client"]
    if not rect:
        return target
    left, top, width, height = rect
    if not (left <= float(x) < left + width and top <= float(y) < top + height):
        return target
    return candidate


def _key_messages(hwnd: int, vk: int, scan: int, *, count: int = 1,
                  delay_ms: int = 0) -> dict:
    """WM_KEYDOWN / WM_KEYUP。lParam 由 ``_key_lparam`` 给（bit30/31 齐全）。"""
    down, up = _key_lparam(scan), _key_lparam(scan, up=True)
    posted: list[str] = []
    for _ in range(max(1, int(count))):
        if not _post(hwnd, WM_KEYDOWN, int(vk), down):
            return {"sent": False, "posted": posted, "reason": "WM_KEYDOWN 被拒绝"}
        posted.append("keydown")
        if delay_ms:
            _sleep(delay_ms / 1000.0)
        if not _post(hwnd, WM_KEYUP, int(vk), up):
            return {"sent": False, "posted": posted, "reason": "WM_KEYUP 被拒绝"}
        posted.append("keyup")
    return {"sent": True, "posted": posted}

def click(x: float | None = None, y: float | None = None, *, hwnd: int = 0,
          window: Any = None, button: str = "left", count: int = 1,
          delay_ms: int = 40, budget_ms: int = DEFAULT_BUDGET_MS,
          prefer: str = "auto", win: Any = None,
          activate: str = "auto") -> dict:
    """在目标窗口的 (x, y)（屏幕坐标）点一下，不激活窗口。

    ``prefer``：``auto``（默认，先 UIA 再消息）/ ``uia`` / ``message``。
    UIA 失败**不会**被当成失败：自动落消息通道（这是本模块的设计承诺）。
    两条都不行才抛 ``BackgroundUnavailable``。

    ``activate``：``auto``（默认，目标不是前台才发 WM_ACTIVATE）/ ``never`` /
    ``always``。它**不**调 SetForegroundWindow，真实前台不动；但极少数应用
    收到这条消息后会自己去抢前台，所以不要默认 always。
    """
    activate_mode = _activate_mode(activate)
    require_running()
    budget = _Budget(budget_ms)
    target = resolve(hwnd=hwnd, window=window, win=win)
    check_target(target, budget=budget)
    if x is None or y is None:
        raise BackgroundUnavailable("后台点击需要目标点坐标 (x, y)", reason="missing_point")
    rect = target["client"]
    if not rect:
        raise BackgroundUnavailable("读不到目标窗口的客户区矩形", reason="no_client_rect")
    left, top, width, height = rect
    if not (left <= float(x) < left + width and top <= float(y) < top + height):
        raise BackgroundUnavailable(
            f"({x}, {y}) 不在窗口 {target['hwnd']:#x} 的客户区内"
            f"（客户区 {left},{top} {width}x{height}）",
            reason="point_outside_window")

    popup: dict | None = None
    swapped = _active_popup_target(target, float(x), float(y), budget=budget)
    if int(swapped["hwnd"]) != int(target["hwnd"]):
        # 有模态弹窗：消息要发给弹窗，坐标换算也以它为准（沿用同一套
        # ScreenToClient，屏幕坐标本身不变）。
        popup = {"from": target["hwnd"], "to": swapped["hwnd"],
                 "cls": swapped.get("cls")}
        target = swapped

    found = descend(target, float(x), float(y), budget=budget)
    leaf = found["leaf"]
    budget.mark("descend_done")
    denied = _denied(str(leaf["cls"]))
    if denied:
        raise BackgroundUnavailable(denied, reason="class_denied")

    timings: dict[str, float] = {}
    t0 = time.monotonic()
    uia_report: dict | None = None
    if prefer != "message" and uia.available():
        # UIA 有**自己的**超时，不共用 Win32 那 800ms 预算：跨进程 Invoke 是同步
        # 调用，实测常要 1s 上下才返回；拿 Win32 预算去卡它只会制造假超时。
        uia_report = uia.invoke(int(leaf["hwnd"]), timeout_ms=uia.DEFAULT_TIMEOUT_MS)
        timings["uia_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
        if uia_report.get("ok"):
            return {
                "ok": True, "delivery": "background", "channel": "uia-invoke",
                "dispatched": True, "hwnd": target["hwnd"], "leaf": leaf,
                "chain": found["chain"], "depth": found["depth"],
                "point": {"x": x, "y": y}, "target": {
                    "hwnd": leaf["hwnd"], "cls": leaf["cls"]},
                "uia": uia_report, "popup": popup, "timings": timings,
            }
        reason = str(uia_report.get("reason") or "")
        if reason not in _UIA_DEFINITIVE_MISSES:
            # timeout / busy / error：**不知道**动作有没有发生。转消息通道就等于
            # 再按一次 —— UIA 的 Invoke 是同步调用，超时那条很可能已经在目标里
            # 执行完了（实测 WinForms 按钮就这样：调用超时返回，按钮却响了）。
            raise BackgroundUnavailable(
                f"UIA 调用结果不确定（{reason or 'unknown'}）：可能已经发生，"
                "不能再投一次消息，否则会点两下。要确认请重新截图比较。",
                reason="uia_unknown", dispatched=None, code="BACKGROUND_UNKNOWN")
    if prefer == "uia":
        raise BackgroundUnavailable(
            f"UIA 通道不可用（{uia_report.get('reason') if uia_report else 'unavailable'}）",
            reason="uia_unavailable", dispatched=False)

    # ── 消息通道 ──
    if not _allowed_control(str(leaf["cls"])) and not _allowed_root(str(leaf["cls"])):
        raise BackgroundUnavailable(
            f"控件类 {leaf['cls']!r} 不在后台消息白名单内；"
            "这类框架不把 posted 消息当输入，强行投递只会得到 dispatched:true "
            "而界面毫无变化。",
            reason="class_not_allowed")
    root_cls = str(target.get("cls") or "")
    if not _allowed_root(root_cls):
        raise BackgroundUnavailable(
            f"根窗口类 {root_cls!r} 不在后台消息白名单内（#32770 / WinForms 之外"
            "一律不猜）", reason="root_not_allowed")
    client = _screen_to_client(int(leaf["hwnd"]), float(x), float(y))
    if client is None:
        raise BackgroundUnavailable("ScreenToClient 失败，坐标语义不可信",
                                    reason="client_point_failed")
    budget.mark("before_post")
    activation = _activate(int(target["hwnd"]), activate_mode)
    timings["activate_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
    posted = _click_messages(int(leaf["hwnd"]), client[0], client[1],
                             button=button, count=count, delay_ms=delay_ms)
    timings["post_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
    if not posted.get("sent"):
        raise BackgroundUnavailable(
            f"PostMessage 被拒绝（{posted.get('reason')}）", reason="post_failed")
    return {
        "ok": True, "delivery": "background", "channel": "message",
        "dispatched": True, "hwnd": target["hwnd"], "leaf": leaf,
        "chain": found["chain"], "depth": found["depth"],
        "point": {"x": x, "y": y}, "target": {"hwnd": leaf["hwnd"], "cls": leaf["cls"]},
        "posted": posted, "uia": uia_report, "popup": popup,
        "activation": activation, "timings": timings,
    }


def describe(*, hwnd: int = 0, window: Any = None, x: float | None = None,
             y: float | None = None, budget_ms: int = DEFAULT_BUDGET_MS,
             win: Any = None) -> dict:
    """只观察不投递：目标是否可后台投递、会落到哪个叶子、UIA 有没有 pattern。

    调试用。它同样不激活窗口、不动光标。
    """
    budget = _Budget(budget_ms)
    target = resolve(hwnd=hwnd, window=window, win=win)
    out: dict[str, Any] = {"target": target, "allowed": True}
    try:
        check_target(target, budget=budget)
    except InputError as exc:
        out.update({"allowed": False, "reason": getattr(exc, "reason", None),
                    "error": str(exc)})
    if x is not None and y is not None:
        found = descend(target, float(x), float(y), budget=budget)
        leaf = found["leaf"]
        out.update({"chain": found["chain"], "leaf": leaf, "depth": found["depth"],
                    "control_allowed": _allowed_control(str(leaf["cls"])),
                    "root_allowed": _allowed_root(str(target.get("cls") or "")),
                    "denied": _denied(str(leaf["cls"]))})
        if uia.available():
            # 和 invoke 一样用自己的超时：UIA 不走 Win32 那 800ms 预算。
            out["uia"] = uia.probe(int(leaf["hwnd"]),
                                   timeout_ms=uia.PROBE_TIMEOUT_MS)
        else:
            out["uia"] = {"ok": False, "reason": "unavailable"}
    return out
