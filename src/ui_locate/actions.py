"""动作原语：把「点一下」「拖一下」「敲个键」变成可调用的一行。

为什么要有这一层，而不是在需要的时候随手调 SendInput：
  1. SendInput 的 `INPUT` 结构体在 x86_64 上是 40 字节、在 x86 上是 28 字节，
     手写一次就容易错；写错的表现是「调用成功但什么都没发生」，很难查。
  2. 鼠标坐标要归一化到 0..65535 且区分虚拟桌面坐标与主屏坐标。副屏原点
     是负数时（本机实测 -1234），这一步算错就会点到别处去。
  3. 键盘要处理扩展键与 Unicode 输入。游戏/安装器里没有输入法，用
     KEYEVENTF_UNICODE 逐码点送比模拟击键可靠。

三条通道，各有各的适用面：

  SendInput    走系统输入队列，最像真人操作，游戏和 DirectInput 程序只认它。
               要求目标窗口在前台。
  PostMessage  把带 lPar​am 坐标的 WM_* 消息直接投给窗口，不需要焦点、不动
               光标，后台也能用。代价是现代框架（Chromium、WPF）通常会忽略
               合成消息。
  SendMessage  与 PostMessage 同上，但同步等目标线程处理完。目标卡住时会一起
               挂，只用于确实需要确认送达的场合。

鼠标移动的两种方式（``pointer``）：

  absolute   ABSOLUTE|VIRTUALDESK，一步到位。桌面程序、窗口化游戏都认。
  relative   只送相对位移（像真鼠标那样「推」过去）。用 Raw Input / DirectInput
             读鼠标的全屏游戏会**忽略**绝对坐标事件，只认相对位移，这时必须用它。
             相对位移会被 Windows 的「提高指针精确度」（加速）放大，所以移动期间
             临时把加速关掉、速度调到 1:1（只改当前会话，不写注册表，结束即恢复），
             再按光标回读闭环修正几轮。游戏把系统光标锁死、回读不到变化时，退回
             「开环」：按 1:1 一次推完，并在结果里标 ``tracking: false``。

按键的两种方式（``keys``）：

  vk         同时填虚拟键码和扫描码（默认）。普通程序认 VK，多数游戏认扫描码。
  scancode   加 KEYEVENTF_SCANCODE，系统只看扫描码。DirectInput 游戏最稳。

`delivery="auto"` 的规则：先 SendInput；若返回值显示一条都没送出去，再退回
PostMessage。**不把「没抛异常」当作成功** —— 这两个 API 都可能返回成功而
什么都没发生，真正的判据在 `ui_locate.act` 的验证阶段。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time

__all__ = [
    "InputError",
    "available",
    "cursor",
    "move",
    "click",
    "scroll",
    "drag",
    "press",
    "hotkey",
    "hold",
    "type_text",
    "window_at",
    "describe",
]


class InputError(RuntimeError):
    """输入通道不可用，或参数组合没有意义。"""


_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:  # pragma: no cover - 只在 Windows 上走真实现
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
else:  # pragma: no cover
    _user32 = None  # type: ignore[assignment]


# ── 结构体 ──────────────────────────────────────────────────────────────────
# 字段顺序与类型必须与 winuser.h 完全一致。任何一个字段写错，SendInput 都会
# 返回「已发送」的数量，但系统拿到的是一堆垃圾 —— 这正是它难查的地方。

_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1

# 鼠标事件标志
_MOVE = 0x0001
_LEFTDOWN = 0x0002
_LEFTUP = 0x0004
_RIGHTDOWN = 0x0008
_RIGHTUP = 0x0010
_MIDDLEDOWN = 0x0020
_MIDDLEUP = 0x0040
_WHEEL = 0x0800
_HWHEEL = 0x1000
_ABSOLUTE = 0x8000
_VIRTUALDESK = 0x4000

_MAX_STEP = 120  # 相对移动每个事件最多推多少像素：有的游戏会截断单次过大的位移

# 键盘事件标志
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_SCANCODE = 0x0008

# SystemParametersInfo
_SPI_GETMOUSE = 0x0003
_SPI_SETMOUSE = 0x0004
_SPI_GETMOUSESPEED = 0x0070
_SPI_SETMOUSESPEED = 0x0071

# 消息参数
_MK_LBUTTON = 0x0001
_MK_RBUTTON = 0x0002
_MK_MBUTTON = 0x0010

_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_WM_MBUTTONDOWN = 0x0207
_WM_MBUTTONUP = 0x0208
_WM_MOUSEWHEEL = 0x020A
_WM_MOUSEHWHEEL = 0x020E


# ── 虚拟键码 ────────────────────────────────────────────────────────────────
# 只收常用键。写全表没有意义，反而让人以为 0x00 到 0xFF 都在这儿 —— 用不到
# 的键请直接传十六进制串，比如 "0x5D"。

_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14,
    "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "insert": 0x2D, "delete": 0x2E,
    "win": 0x5B,
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "numlock": 0x90, "scrolllock": 0x91,
    "shiftleft": 0xA0, "shiftright": 0xA1,
    "ctrlleft": 0xA2, "ctrlright": 0xA3,
    "altleft": 0xA4, "altright": 0xA5,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
for _i in range(24):
    _VK[f"f{_i + 1}"] = 0x70 + _i
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    _VK[_c] = ord(_c.upper())

# 这些键在扫描码层面是「扩展键」，不标会被当成小键盘上的同一个键
_EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x21, 0x22, 0x23, 0x24, 0x2D, 0x2E, 0x5B, 0x5D}

# 常见别名
_ALIAS = {"esc": "escape", "del": "delete", "return": "enter", "control": "ctrl",
          "ins": "insert", "pgup": "pageup", "pgdn": "pagedown", "meta": "win",
          "super": "win", "cmd": "win"}


def available() -> bool:
    """当前平台能不能注入输入。"""
    return _IS_WINDOWS


def _require() -> None:
    if not _IS_WINDOWS:
        raise InputError(f"输入注入只支持 Windows，当前是 {sys.platform}")


# ── 基础发送 ────────────────────────────────────────────────────────────────

def _send(*items: _INPUT) -> int:
    """把一串事件一次性交给 SendInput，返回系统接受了几个。

    必须一次送完：单击的按下与抬起之间若被别的调用插入，目标可能把它当成
    两次独立单击（双击判定窗口通常 500ms 内，但中间插入的事件会打断）。
    """
    _require()
    if not items:
        return 0
    array = (_INPUT * len(items))(*items)
    ctypes.set_last_error(0)
    sent = int(_user32.SendInput(len(items), array, ctypes.sizeof(_INPUT)))
    if sent == 0:
        raise InputError(
            f"SendInput 一条事件都没送出去（{len(items)} 条全部失败，"
            f"last_error={ctypes.get_last_error()}）。"
            "通常是会话被锁、UIPI 拦截，或进程不在交互式桌面里。"
        )
    return sent


def _mouse_event(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    item = _INPUT()
    item.type = _INPUT_MOUSE
    item.u.mi = _MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return item


def key_mode(want: str | None = None) -> str:
    """vk / scancode。参数 > 环境变量 UI_LOCATE_KEYS > runtime.json "keys" > vk。"""
    return _mode(want, "UI_LOCATE_KEYS", "keys", ("vk", "scancode"), "vk")


def pointer_mode(want: str | None = None) -> str:
    """absolute / relative。参数 > UI_LOCATE_POINTER > runtime.json "pointer" > absolute。"""
    return _mode(want, "UI_LOCATE_POINTER", "pointer", ("absolute", "relative"), "absolute")


def _mode(want: str | None, env: str, key: str, allowed: tuple[str, ...], default: str) -> str:
    import os

    value = str(want or "").strip().lower()
    if not value:
        value = os.environ.get(env, "").strip().lower()
    if not value:
        try:
            from ui_locate.config import runtime_settings

            raw = runtime_settings().get(key)
            value = raw.strip().lower() if isinstance(raw, str) else ""
        except Exception:  # noqa: BLE001
            value = ""
    return value if value in allowed else default


def _scan_code(vk: int) -> int:
    """VK → 扫描码（MAPVK_VK_TO_VSC）。拿不到就 0。"""
    if not _IS_WINDOWS:
        return 0
    try:
        return int(_user32.MapVirtualKeyW(vk, 0)) & 0xFFFF
    except Exception:  # noqa: BLE001
        return 0


def _key_event(vk: int, up: bool = False, *, extended: bool = False,
               unicode: bool = False, mode: str = "vk") -> _INPUT:
    item = _INPUT()
    item.type = _INPUT_KEYBOARD
    flags = 0
    if up:
        flags |= _KEYEVENTF_KEYUP
    if extended:
        flags |= _KEYEVENTF_EXTENDEDKEY
    if unicode:
        flags |= _KEYEVENTF_UNICODE
        # unicode 模式下 wVk 必须为 0，字符放在 wScan 里
        item.u.ki = _KEYBDINPUT(0, vk, flags, 0, 0)
        return item
    # 扫描码总是填上：wScan=0 时，读扫描码的游戏（DirectInput / Raw Input）
    # 收到的是「一个不存在的键」，表现为按了没反应。
    scan = _scan_code(vk)
    if mode == "scancode" and scan:
        flags |= _KEYEVENTF_SCANCODE
    item.u.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    return item


# ── 坐标换算 ────────────────────────────────────────────────────────────────

def _virtual_rect() -> tuple[int, int, int, int]:
    """虚拟桌面矩形（含所有显示器）。副屏在主屏左侧时 x 是负数。"""
    _require()
    get = _user32.GetSystemMetrics
    return (
        get(76), get(77), get(78), get(79),  # SM_X/Y/CX/CYVIRTUALSCREEN
    )


def _normalize(x: int, y: int) -> tuple[int, int]:
    """屏幕绝对坐标 → SendInput 要的 0..65535 归一化值。

    用虚拟桌面（而不是主屏）做基准，并配上 MOUSEEVENTF_VIRTUALDESK：否则
    副屏上的点会被映射主屏范围内，整体偏移一整块屏。
    """
    vx, vy, vw, vh = _virtual_rect()
    if vw <= 1 or vh <= 1:
        raise InputError(f"虚拟桌面尺寸异常：{vw}x{vh}")
    nx = int(round((x - vx) * 65535 / (vw - 1)))
    ny = int(round((y - vy) * 65535 / (vh - 1)))
    # 允许贴边，但不允许越界 —— 越界会被系统夹到边上，看起来像"点偏了"
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def cursor() -> dict:
    """读回光标位置。目标程序把光标锁住时这个值不会变，只能作参考。"""
    _require()
    pt = wt.POINT()
    if not _user32.GetCursorPos(ctypes.byref(pt)):
        raise InputError(f"GetCursorPos 失败（last_error={ctypes.get_last_error()}）")
    return {"x": int(pt.x), "y": int(pt.y)}


def _cursor_or_none() -> dict | None:
    """尽力读一次光标；读不到返回 None。

    为什么要有这个宽容版本：`cursor()` 在诊断场景下应该报错（读不到就是
    读不到，别装没事），但点击流程里读光标只是为了「报告光标动没动」——
    这是个观测，不是前置条件。本机实测这个调用会偶发失败（同一个进程里
    前一次成功、后一次 err=5），让它把整次点击带崩毫无道理。
    """
    try:
        return cursor()
    except InputError:
        return None


def window_at(x: int, y: int) -> dict:
    """这个屏幕坐标下面是哪个窗口。用来确认「我要点的地方确实是它」。"""
    _require()
    hwnd = int(_user32.WindowFromPoint(wt.POINT(int(x), int(y))) or 0)
    if not hwnd:
        return {"hwnd": 0, "class": None, "title": None}
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    cls = buf.value
    _user32.GetWindowTextW(hwnd, buf, 256)
    return {"hwnd": hwnd, "class": cls, "title": buf.value}


# ── 相对移动 ────────────────────────────────────────────────────────────────

class _NoAccel:
    """with 块内关掉鼠标加速、速度设为 10（1:1），退出时原样恢复。

    fWinIni=0：只改当前会话的内存值，不写注册表、不广播 —— 进程崩了也不会把
    用户的鼠标设置永久改掉（重新登录即复原）。
    """

    def __init__(self) -> None:
        self.saved_accel = None
        self.saved_speed = None

    def __enter__(self) -> "_NoAccel":
        if not _IS_WINDOWS:
            return self
        try:
            accel = (ctypes.c_int * 3)()
            if _user32.SystemParametersInfoW(_SPI_GETMOUSE, 0, accel, 0):
                self.saved_accel = list(accel)
                _user32.SystemParametersInfoW(_SPI_SETMOUSE, 0, (ctypes.c_int * 3)(0, 0, 0), 0)
            speed = ctypes.c_int()
            if _user32.SystemParametersInfoW(_SPI_GETMOUSESPEED, 0, ctypes.byref(speed), 0):
                self.saved_speed = int(speed.value)
                _user32.SystemParametersInfoW(_SPI_SETMOUSESPEED, 0, ctypes.c_void_p(10), 0)
        except Exception:  # noqa: BLE001 - 关不掉加速就带着加速走，闭环会修正
            pass
        return self

    def __exit__(self, *exc: object) -> None:
        if not _IS_WINDOWS:
            return
        try:
            if self.saved_accel is not None:
                _user32.SystemParametersInfoW(
                    _SPI_SETMOUSE, 0, (ctypes.c_int * 3)(*self.saved_accel), 0)
            if self.saved_speed is not None:
                _user32.SystemParametersInfoW(
                    _SPI_SETMOUSESPEED, 0, ctypes.c_void_p(self.saved_speed), 0)
        except Exception:  # noqa: BLE001
            pass


def _split(d: int, limit: int = _MAX_STEP) -> list[int]:
    """把一段位移拆成每步不超过 limit 的若干步，和等于 d。"""
    if d == 0:
        return []
    n = (abs(d) + limit - 1) // limit
    base, rest = divmod(abs(d), n)
    sign = 1 if d > 0 else -1
    return [sign * (base + (1 if i < rest else 0)) for i in range(n)]


def push_relative(dx: int, dy: int, send, *, pause_s: float = 0.002) -> int:
    """把 (dx, dy) 拆成若干个相对 MOVE 事件送出去，返回送出的条数。"""
    xs, ys = _split(dx), _split(dy)
    n = max(len(xs), len(ys))
    xs += [0] * (n - len(xs))
    ys += [0] * (n - len(ys))
    sent = 0
    for sx, sy in zip(xs, ys):
        sent += send(_mouse_event(_MOVE, sx, sy))
        if pause_s:
            time.sleep(pause_s)
    return sent


def seek_relative(x: int, y: int, *, read, send, rounds: int = 6,
                  tolerance: int = 1, pause_s: float = 0.002,
                  settle_s: float = 0.01) -> dict:
    """用相对位移把光标推到 (x, y)，按回读闭环修正。

    ``read()`` 返回 {"x","y"} 或 None；``send(event)`` 返回送出条数。拆成参数是
    为了不在 Windows 上也能测这段控制逻辑。

    - 回读得到、而且光标真的跟着动：最多 ``rounds`` 轮，误差 ≤ tolerance 就停
    - 第一轮推完光标纹丝不动（游戏锁死 / 隐藏了系统光标）：判定为不可追踪，
      已经按 1:1 推过一次了，不再重复推，``tracking: false``
    - 回读不到：只推一次，``tracking: null``
    """
    start = read()
    if start is None:
        # 不知道在哪，就没法算位移 —— 这种情况调用方应改用 absolute
        return {"sent": 0, "tracking": None, "rounds": 0, "error": "读不到光标位置"}
    cur = start
    sent = 0
    gain = [1.0, 1.0]  # 实测「推 1 像素光标走几像素」，用来抵消没关掉的加速
    for i in range(rounds):
        dx, dy = int(x) - int(cur["x"]), int(y) - int(cur["y"])
        if abs(dx) <= tolerance and abs(dy) <= tolerance:
            return {"sent": sent, "tracking": True, "rounds": i, "cursor": cur, "error_px": [dx, dy]}
        cx_cmd = int(round(dx / gain[0]))
        cy_cmd = int(round(dy / gain[1]))
        sent += push_relative(cx_cmd, cy_cmd, send, pause_s=pause_s)
        if settle_s:
            time.sleep(settle_s)
        nxt = read()
        if nxt is None:
            return {"sent": sent, "tracking": None, "rounds": i + 1, "cursor": None}
        if (nxt["x"], nxt["y"]) == (cur["x"], cur["y"]):
            return {"sent": sent, "tracking": False, "rounds": i + 1, "cursor": nxt,
                    "note": "系统光标没跟着动（游戏锁定/隐藏了光标），已按 1:1 开环推完"}
        for k, cmd, moved in ((0, cx_cmd, nxt["x"] - cur["x"]), (1, cy_cmd, nxt["y"] - cur["y"])):
            if abs(cmd) >= 8 and moved * cmd > 0:  # 位移太小时估计不准，不更新
                gain[k] = max(0.25, min(4.0, moved / cmd))
        cur = nxt
    dx, dy = int(x) - int(cur["x"]), int(y) - int(cur["y"])
    return {"sent": sent, "tracking": True, "rounds": rounds, "cursor": cur, "error_px": [dx, dy]}


def _goto(x: int, y: int, pointer: str) -> dict:
    """按 pointer 模式把光标送到 (x, y)。relative 时返回 seek 报告，absolute 返回 {}。"""
    if pointer != "relative":
        nx, ny = _normalize(x, y)
        _send(_mouse_event(_MOVE | _ABSOLUTE | _VIRTUALDESK, nx, ny))
        return {}
    with _NoAccel():
        return seek_relative(x, y, read=_cursor_or_none, send=_send)


# ── 鼠标 ────────────────────────────────────────────────────────────────────

def move(x: int, y: int, *, settle_ms: int = 0, pointer: str | None = None) -> dict:
    """把光标移到屏幕绝对坐标。

    `Moved` 只是「事件送出去了」；本机实测某些进程里 SendInput 返回成功但
    光标不动（被外层拦），所以这里额外回读一次光标，把两件事分开报。
    """
    _require()
    mode = pointer_mode(pointer)
    if mode == "relative":
        rel = _goto(x, y, "relative")
        if settle_ms:
            time.sleep(max(0, settle_ms) / 1000.0)
        return {"sent": rel.get("sent", 0), "x": x, "y": y, "pointer": "relative",
                "relative": rel, "cursor": _cursor_or_none(),
                "cursor_moved": rel.get("tracking")}
    nx, ny = _normalize(x, y)
    before = _cursor_or_none()
    sent = _send(_mouse_event(_MOVE | _ABSOLUTE | _VIRTUALDESK, nx, ny))
    if settle_ms:
        time.sleep(max(0, settle_ms) / 1000.0)
    after = _cursor_or_none()
    moved = None
    if before is not None and after is not None:
        moved = (after["x"], after["y"]) != (before["x"], before["y"])
    return {
        "sent": sent,
        "x": x, "y": y,
        "cursor": after,
        "cursor_moved": moved,
    }


_BUTTON_FLAGS = {
    "left": (_LEFTDOWN, _LEFTUP, _MK_LBUTTON, _WM_LBUTTONDOWN, _WM_LBUTTONUP),
    "right": (_RIGHTDOWN, _RIGHTUP, _MK_RBUTTON, _WM_RBUTTONDOWN, _WM_RBUTTONUP),
    "middle": (_MIDDLEDOWN, _MIDDLEUP, _MK_MBUTTON, _WM_MBUTTONDOWN, _WM_MBUTTONUP),
}


def click(x: int, y: int, *, button: str = "left", count: int = 1,
          delivery: str = "auto", delay_ms: int = 40, pointer: str | None = None) -> dict:
    """在屏幕绝对坐标点一下。

    delivery:
      "send_input"  只走系统队列（最像真人，要求前台）
      "message"     只投窗口消息（不动光标、不需焦点，但很多框架不认）
      "auto"        先试 send_input；光标没动过就再投一次消息（默认）
    """
    _require()
    btn = str(button or "left").strip().lower()
    if btn not in _BUTTON_FLAGS:
        raise InputError(f"不认识的鼠标键 {button!r}；可用 left / right / middle")
    count = max(1, int(count))

    mode = str(delivery or "auto").strip().lower()
    if mode in ("sendinput", "send", "input"):
        mode = "send_input"
    if mode in ("post", "postmessage", "msg"):
        mode = "message"
    if mode not in ("send_input", "message", "auto"):
        raise InputError(f"不认识的 delivery {delivery!r}；可用 send_input / message / auto")

    down, up, mkflag, down_msg, up_msg = _BUTTON_FLAGS[btn]
    report: dict = {"x": x, "y": y, "button": btn, "count": count}

    if pointer_mode(pointer) == "relative" and mode != "message":
        # 全屏游戏：先「推」到位，再原地按下抬起。不退回窗口消息 ——
        # 这类游戏本来就不认合成消息，退回去只会多一次无效点击。
        rel = _goto(x, y, "relative")
        events = []
        for _ in range(count):
            events.append(_mouse_event(down))
            events.append(_mouse_event(up))
        report["pointer"] = "relative"
        report["relative"] = rel
        report["send_input"] = {"sent": _send(*events), "cursor_moved": rel.get("tracking")}
        return report

    if mode in ("send_input", "auto"):
        nx, ny = _normalize(x, y)
        before = _cursor_or_none()
        events: list[_INPUT] = [_mouse_event(_MOVE | _ABSOLUTE | _VIRTUALDESK, nx, ny)]
        for _ in range(count):
            events.append(_mouse_event(down))
            events.append(_mouse_event(up))
        sent = _send(*events)
        after = _cursor_or_none()
        moved = None
        if before is not None and after is not None:
            moved = (after["x"], after["y"]) != (before["x"], before["y"])
        report["send_input"] = {"sent": sent, "cursor_moved": moved}
        # 只有**明确观察到**光标没动，才认为这条通道没生效、继续试消息通道。
        # moved is None（光标读不到）时必须当作"不知道"而不是"没动"：
        # 本机实测读光标偶发失败，据此退回 message 会白白丢掉一次已经成功的点击。
        if mode == "send_input" or moved is not False or count > 1:
            return report

    report["message"] = _click_message(x, y, mkflag, down_msg, up_msg, count, delay_ms)
    return report


def _click_message(x: int, y: int, mkflag: int, down: int, up: int,
                   count: int, delay_ms: int) -> dict:
    """把带 lParam 坐标的鼠标消息直接投给那个坐标上的窗口。"""
    target = window_at(x, y)
    hwnd = target["hwnd"]
    if not hwnd:
        return {"sent": False, "reason": "该坐标上没有窗口"}

    pt = wt.POINT(int(x), int(y))
    _user32.ScreenToClient(hwnd, ctypes.byref(pt))
    lparam = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)

    delay = max(0, int(delay_ms)) / 1000.0
    accepted = 0
    total = 0
    for _ in range(count):
        for msg, wparam in ((_WM_MOUSEMOVE, 0), (down, mkflag), (up, 0)):
            total += 1
            if _user32.PostMessageW(hwnd, msg, wparam, lparam):
                accepted += 1
            if delay:
                time.sleep(delay)
    return {
        "sent": accepted == total,
        "accepted": accepted,
        "total": total,
        "window": {"hwnd": hwnd, "class": target["class"], "title": target["title"]},
        "client": {"x": int(pt.x), "y": int(pt.y)},
    }


def scroll(x: int, y: int, *, delta: int = -3, horizontal: bool = False,
           pointer: str | None = None) -> dict:
    """在坐标处滚轮。delta 为负是向下/向右滚，一档通常是 120。"""
    _require()
    amount = int(delta) * 120
    flag = _HWHEEL if horizontal else _WHEEL
    _goto(x, y, pointer_mode(pointer))
    sent = _send(_mouse_event(flag, 0, 0, amount))
    return {"sent": sent, "x": x, "y": y, "delta": delta, "horizontal": bool(horizontal)}


def drag(x1: int, y1: int, x2: int, y2: int, *, button: str = "left",
         steps: int = 12, duration_ms: int = 300, pointer: str | None = None) -> dict:
    """从 (x1,y1) 拖到 (x2,y2)。

    中间必须插 MOUSEMOVE：只在起点按下、终点抬起，很多程序当成「没动过」
    而忽略整个手势。`steps` 决定插几个中间点。
    """
    _require()
    btn = str(button or "left").strip().lower()
    if btn not in _BUTTON_FLAGS:
        raise InputError(f"不认识的鼠标键 {button!r}；可用 left / right / middle")
    down, up, _mkflag, _dm, _um = _BUTTON_FLAGS[btn]

    steps = max(1, int(steps))
    pause = max(0, int(duration_ms)) / 1000.0 / steps

    if pointer_mode(pointer) == "relative":
        with _NoAccel():
            rel = seek_relative(x1, y1, read=_cursor_or_none, send=_send)
            sent = rel.get("sent", 0) + _send(_mouse_event(down))
            # 按住后分段推：每段推一截，最后一段按回读修正余量
            px, py = x1, y1
            for i in range(1, steps + 1):
                tx = int(round(x1 + (x2 - x1) * i / steps))
                ty = int(round(y1 + (y2 - y1) * i / steps))
                sent += push_relative(tx - px, ty - py, _send)
                px, py = tx, ty
                if pause:
                    time.sleep(pause)
            if rel.get("tracking"):
                fix = seek_relative(x2, y2, read=_cursor_or_none, send=_send, rounds=3)
                sent += fix.get("sent", 0)
            sent += _send(_mouse_event(up))
        return {"sent": sent, "from": [x1, y1], "to": [x2, y2], "button": btn,
                "steps": steps, "pointer": "relative", "tracking": rel.get("tracking")}

    nx1, ny1 = _normalize(x1, y1)
    nx2, ny2 = _normalize(x2, y2)
    sent = _send(_mouse_event(_MOVE | _ABSOLUTE | _VIRTUALDESK, nx1, ny1),
                 _mouse_event(down))

    # 中间点一律用「绝对移动」：相对移动在 MOUSEEVENTF_ABSOLUTE 未设时按
    # 归一化单位解释，跟像素不是一回事，很容易累积出几像素到几十像素的偏差。
    for i in range(1, steps + 1):
        t = i / steps
        nx = int(round(nx1 + (nx2 - nx1) * t))
        ny = int(round(ny1 + (ny2 - ny1) * t))
        sent += _send(_mouse_event(_MOVE | _ABSOLUTE | _VIRTUALDESK, nx, ny))
        if pause:
            time.sleep(pause)

    sent += _send(_mouse_event(up))
    return {"sent": sent, "from": [x1, y1], "to": [x2, y2],
            "button": btn, "steps": steps}


# ── 键盘 ────────────────────────────────────────────────────────────────────

def _parse_key(name: str) -> tuple[int, bool]:
    """键名 → (虚拟键码, 是否扩展键)。认不出就抛 InputError。"""
    raw = str(name or "").strip()
    if not raw:
        raise InputError("键名不能为空")
    if raw.lower().startswith("0x"):
        try:
            code = int(raw, 16)
        except ValueError as exc:
            raise InputError(f"看不懂的十六进制键码 {name!r}") from exc
        return code, code in _EXTENDED
    key = _ALIAS.get(raw.lower(), raw.lower())
    code = _VK.get(key)
    if code is None:
        raise InputError(f"不认识的键 {name!r}。常用键名见 actions._VK，或用 '0x1B' 这种写法")
    return code, code in _EXTENDED


def _parse_combo(keys: str) -> list[str]:
    """"ctrl+shift+s" → ["ctrl", "shift", "s"]。"""
    text = str(keys or "").strip()
    if not text:
        raise InputError("快捷键不能为空")
    parts = [p.strip() for p in text.replace(" ", "+").split("+")]
    parts = [p for p in parts if p]
    if not parts:
        raise InputError(f"快捷键 {keys!r} 解析后没有按键")
    return parts


def press(key: str, *, keys_mode: str | None = None, hold_ms: int = 0) -> dict:
    """敲一下某个键（按下再抬起）。

    hold_ms > 0 时按住这么久再抬起。游戏按帧轮询键盘状态，按下抬起挨在一起
    （同一帧内）常常整个漏掉；默认也会在中间留 30ms，约两帧。
    """
    _require()
    mode = key_mode(keys_mode)
    code, extended = _parse_key(key)
    sent = _send(_key_event(code, extended=extended, mode=mode))
    time.sleep(max(30, int(hold_ms or 0)) / 1000.0)
    sent += _send(_key_event(code, up=True, extended=extended, mode=mode))
    return {"sent": sent, "key": key, "vk": code, "keys_mode": mode,
            "hold_ms": max(30, int(hold_ms or 0))}


def hold(keys: str, *, duration_ms: int = 500, keys_mode: str | None = None) -> dict:
    """按住一个键或一组键一段时间（"w"、"shift+w"），再一起松开。

    游戏里走路、蓄力、长按确认都要这个：敲一下只走半步。
    """
    _require()
    mode = key_mode(keys_mode)
    parsed = [_parse_key(p) for p in _parse_combo(keys)]
    duration = max(0, min(30000, int(duration_ms)))
    sent = 0
    try:
        for code, extended in parsed:
            sent += _send(_key_event(code, extended=extended, mode=mode))
        time.sleep(duration / 1000.0)
    finally:
        # 无论如何都要松开：卡住的 W 键会让角色一直走下去
        for code, extended in reversed(parsed):
            sent += _send(_key_event(code, up=True, extended=extended, mode=mode))
    return {"sent": sent, "keys": keys, "duration_ms": duration, "keys_mode": mode}


def hotkey(keys: str, *, keys_mode: str | None = None) -> dict:
    """敲一组组合键，如 "ctrl+shift+s"。

    修饰键先按、后放，且**逆序**抬起：先放主键再放修饰键，否则在浏览器里
    常会触发「修饰键单独按下」的其他行为。
    """
    _require()
    mode = key_mode(keys_mode)
    parts = _parse_combo(keys)
    parsed = [(_parse_key(p)) for p in parts]

    sent = 0
    for code, extended in parsed:
        sent += _send(_key_event(code, extended=extended, mode=mode))
    time.sleep(0.03)  # 同 press：给按帧轮询的游戏留出至少一帧
    for code, extended in reversed(parsed):
        sent += _send(_key_event(code, up=True, extended=extended, mode=mode))
    return {"sent": sent, "keys": keys, "vk": [c for c, _e in parsed], "keys_mode": mode}


def type_text(text: str, *, delay_ms: int = 12) -> dict:
    """逐字符输入文本。

    走 KEYEVENTF_UNICODE：直接把 UTF-16 码元塞进按键事件，不经过键盘布局
    和输入法。对中文、符号、以及没装对应输入法的机器都能用。
    """
    _require()
    chunk = str(text or "")
    if not chunk:
        return {"sent": 0, "length": 0}

    delay = max(0, int(delay_ms)) / 1000.0
    sent = 0
    for ch in chunk:
        # 非 BMP 字符（emoji 等）在 Python 里是单个码点，但 UTF-16 需要代理对。
        # 按 utf-16-le 拆成码元逐个送，目标程序拼回来是一样的。
        raw = ch.encode("utf-16-le")
        units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
        for unit in units:
            sent += _send(_key_event(unit, unicode=True), _key_event(unit, up=True, unicode=True))
            if delay:
                time.sleep(delay)
    return {"sent": sent, "length": len(chunk)}


# ── 诊断 ────────────────────────────────────────────────────────────────────

def describe() -> dict:
    """当前平台的输入能力概况，给排障用。"""
    info: dict = {"platform": sys.platform, "inject": _IS_WINDOWS,
                  "pointer": pointer_mode(), "keys": key_mode()}
    if not _IS_WINDOWS:
        return info
    try:
        vx, vy, vw, vh = _virtual_rect()
        info["virtual"] = {"x": vx, "y": vy, "w": vw, "h": vh}
    except InputError as exc:
        info["virtual"] = f"不可用：{exc}"
    try:
        info["cursor"] = cursor()
    except InputError as exc:
        info["cursor"] = f"不可用：{exc}"
    try:
        from ui_locate import screen

        info["dpi_awareness"] = screen.set_dpi_aware()
    except Exception as exc:  # noqa: BLE001 - 诊断函数不该因为探针失败而失败
        info["dpi_awareness"] = f"未知：{exc}"
    return info
