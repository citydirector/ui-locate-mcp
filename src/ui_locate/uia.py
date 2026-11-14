"""最小 UIA 适配器：raw ctypes COM + 一条独立的 STA 线程。

为什么不用 comtypes / pywinauto
-------------------------------
``comtypes`` 第一次 import 会往用户目录（``%APPDATA%\\Python``）生成
``comtypes.gen`` 包。在受限进程里那一步是 ``PermissionError``，整个 UIA 能力
直接消失 —— 而生成代码本身对我们没有任何价值。手写 vtable 调用不写盘、不注册、
不留生成物，失败时也只是"这条通道不可用"。

线程模型
--------
COM 的 STA 要求对象在创建它的线程里使用，而跨进程的 UIA 调用**可能永远不返回**
（目标进程无响应、跨会话、驱动级拦截）。所以所有 UIA 调用都排进这里的一条
后台线程，调用方按超时等结果：

  * 超时 → 当"这条通道不可用"，转窗口消息或明确拒绝；
  * 超时后线程仍可能卡在旧调用里 → 之后的新请求直接回 ``busy``，不再排队。
    宁可少做一次，也不让一个卡死的队列吞掉后续所有动作。

只在本进程内使用（不 fork）。非 Windows 上 ``available()`` 恒为 False。
"""

from __future__ import annotations

import ctypes
import queue
import threading
import uuid
from typing import Any

HRESULT = ctypes.c_long

S_OK = 0
CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 2
# UIA 客户端必须是 MTA：STA 线程在等这类跨进程调用时需要自己泵消息，而我们
# 这个 worker 只在队列上等活 —— 实测 STA 下 probe 一个普通 #32770 对话框会
# 一直挂到超时，并且把 worker 永久卡成 busy。MTA 下同一调用只要几十毫秒。
COINIT_MULTITHREADED = 0

# UIA 的 pattern id（UIAutomationClient.h）
UIA_InvokePatternId = 10000
UIA_ValuePatternId = 10002

# invoke 是**同步**的：实测一个 WinForms 按钮的 Invoke 要 2021ms 才返回（动作
# 在目标里早已生效）。预算给紧了只会把"成功"报成"不知道"。
DEFAULT_TIMEOUT_MS = 5000
# probe 只是读 pattern/名字：健康时 20ms 量级就回来了，卡住就是真卡住。
PROBE_TIMEOUT_MS = 1200

_WINDLL = getattr(ctypes, "WinDLL", None)
_ole32 = _WINDLL("ole32", use_last_error=True) if _WINDLL is not None else None


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    value = uuid.UUID(text)
    out = _GUID()
    out.Data1 = value.time_low
    out.Data2 = value.time_mid
    out.Data3 = value.time_hi_version
    raw = value.bytes
    for i in range(8):
        out.Data4[i] = raw[8 + i]
    return out


CLSID_CUIAutomation = _guid("ff48dba4-60ef-4201-aa87-54103eef594e")
IID_IUIAutomation = _guid("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee")

# IUnknown 占 0..2，之后才是各接口自己的方法。
_VT_RELEASE = 2
_UIA_ELEMENT_FROM_HANDLE = 6
_EL_GET_CURRENT_PATTERN = 16
_EL_GET_PROCESS_ID = 20
_EL_GET_CONTROL_TYPE = 21
_EL_GET_NAME = 23
_INVOKE = 3
_SET_VALUE = 3

if _ole32 is not None:
    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    _ole32.CoInitializeEx.restype = HRESULT
    _ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    _ole32.CoCreateInstance.restype = HRESULT


def _vtable(obj: int) -> Any:
    return ctypes.cast(ctypes.c_void_p(obj),
                       ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents


def _call(obj: int, index: int, *argtypes: Any, rest: Any = HRESULT) -> Any:
    """取 vtable 第 ``index`` 个方法的可调用包装（argtypes 不含 this）。"""
    proto = ctypes.WINFUNCTYPE(rest, ctypes.c_void_p, *argtypes)
    return proto(_vtable(obj)[index])


def _release(ptr: int | None) -> None:
    """COM 引用计数减一。失败无所谓：进程里泄漏一个引用比抛异常好。"""
    if not ptr:
        return
    try:
        _call(ptr, _VT_RELEASE, rest=ctypes.c_ulong)(ctypes.c_void_p(ptr))
    except Exception:  # noqa: BLE001 - 释放失败不影响动作结果
        pass


class _Worker:
    """一条 COM 线程 + 一个队列。不对外暴露，模块级单例。"""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self.alive = False
        self.error: str | None = None
        self.busy = False
        self.calls = 0
        self.retired = False
        self._uia: int | None = None
        self._thread = threading.Thread(target=self._run, name="ui-locate-uia", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(2.0)

    def _run(self) -> None:
        try:
            if _ole32 is None:
                raise RuntimeError("ole32 不可用（非 Windows 或进程受限）")
            hr = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
            if hr not in (0, 1):  # S_OK / S_FALSE(已初始化)
                raise RuntimeError(f"CoInitializeEx 失败 hr={hr:#x}")
            ptr = ctypes.c_void_p()
            hr = _ole32.CoCreateInstance(
                ctypes.byref(CLSID_CUIAutomation), None, CLSCTX_INPROC_SERVER,
                ctypes.byref(IID_IUIAutomation), ctypes.byref(ptr))
            if hr != S_OK or not ptr.value:
                raise RuntimeError(f"CoCreateInstance(CUIAutomation) 失败 hr={hr:#x}")
            self._uia = ptr.value
            self.alive = True
        except BaseException as exc:  # noqa: BLE001 - 任何失败都只代表"通道不可用"
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._ready.set()
        if not self.alive:
            return
        while True:
            job = self._q.get()
            if job is None:
                return
            fn, box = job
            try:
                box["result"] = fn(self._uia)
            except BaseException as exc:  # noqa: BLE001
                box["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                self.busy = False
                box["done"].set()


_lock = threading.Lock()
_worker: _Worker | None = None
_retired = 0


def _retire(w: _Worker, why: str) -> None:
    """放弃这条线程，下次调用换一条新的。

    卡在跨进程调用里的线程**杀不掉**，但也不能让它把整个 UIA 通道永久拖死：
    实测一个不答 UIA 的合成对话框会让 ElementFromHandle 挂到超时，之后连
    WinForms 窗口也一起 busy。旧线程留着（daemon），只是不再投活进去。
    """
    global _worker, _retired
    with _lock:
        w.retired = True
        _retired += 1
        if _worker is w:
            _worker = None
    _ = why


def worker() -> _Worker:
    global _worker
    with _lock:
        if _worker is None:
            _worker = _Worker()
            _worker.start()
        return _worker


def available() -> bool:
    """UIA 通道能不能用（创建 COM 对象成功）。不检查目标窗口。"""
    return bool(worker().alive)


def state() -> dict:
    w = worker()
    return {"available": w.alive, "busy": w.busy, "error": w.error,
            "calls": w.calls, "retired": _retired}


def _submit(fn: Any, timeout_ms: int) -> dict:
    w = worker()
    if not w.alive:
        return {"ok": False, "reason": "unavailable",
                "detail": w.error or "UIA 线程没有起来"}
    if w.busy:
        return {"ok": False, "reason": "busy",
                "detail": "上一次 UIA 调用还没返回（可能卡在目标进程）"}
    box: dict[str, Any] = {"done": threading.Event(), "result": None, "error": None}
    w.busy = True
    w.calls += 1
    w._q.put((fn, box))
    if not box["done"].wait(max(0.05, timeout_ms / 1000.0)):
        # 这条线程多半还卡在这个调用里：不放新活进去（会把队列堵死），
        # 也不复用它 —— 直接退休，下一次调用换一条新线程。
        _retire(w, "timeout")
        return {"ok": False, "reason": "timeout",
                "detail": f"UIA 调用超过 {timeout_ms}ms 未返回，已放弃"}
    if box["error"]:
        return {"ok": False, "reason": "error", "detail": box["error"]}
    return box["result"] or {"ok": False, "reason": "no_result"}


def _element(uia: int, hwnd: int) -> tuple[int, int]:
    out = ctypes.c_void_p()
    fn = _call(uia, _UIA_ELEMENT_FROM_HANDLE, ctypes.c_void_p,
               ctypes.POINTER(ctypes.c_void_p))
    hr = fn(ctypes.c_void_p(uia), ctypes.c_void_p(hwnd), ctypes.byref(out))
    return int(out.value or 0), int(hr)


def _pattern(element: int, pattern_id: int) -> tuple[int, int]:
    out = ctypes.c_void_p()
    fn = _call(element, _EL_GET_CURRENT_PATTERN, ctypes.c_int,
               ctypes.POINTER(ctypes.c_void_p))
    hr = fn(ctypes.c_void_p(element), int(pattern_id), ctypes.byref(out))
    return int(out.value or 0), int(hr)


def _describe(element: int) -> dict:
    """控制类型 + 名字，只用于诊断信息（不参与判定）。"""
    out: dict[str, Any] = {}
    try:
        ctype_value = ctypes.c_int()
        fn = _call(element, _EL_GET_CONTROL_TYPE, ctypes.POINTER(ctypes.c_int))
        if fn(ctypes.c_void_p(element), ctypes.byref(ctype_value)) == S_OK:
            out["control_type"] = int(ctype_value.value)
    except Exception:  # noqa: BLE001
        pass
    try:
        name = ctypes.c_wchar_p()
        fn = _call(element, _EL_GET_NAME, ctypes.POINTER(ctypes.c_wchar_p))
        if fn(ctypes.c_void_p(element), ctypes.byref(name)) == S_OK and name.value:
            out["name"] = name.value
    except Exception:  # noqa: BLE001
        pass
    return out


def _with_element(hwnd: int, body: Any) -> dict:
    """在 UIA 线程里取元素 → 交给 body(element) → 一定释放。"""

    def job(uia: int) -> dict:
        element, hr = _element(uia, hwnd)
        if not element:
            return {"ok": False, "reason": "no_element",
                    "detail": f"ElementFromHandle({hwnd:#x}) hr={hr:#x}"}
        try:
            return body(element)
        finally:
            _release(element)

    return job


def invoke(hwnd: int, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict:
    """对 ``hwnd`` 调 InvokePattern（按钮、菜单项这类"按一下"的元素）。"""

    def body(element: int) -> dict:
        info = _describe(element)
        pattern, hr = _pattern(element, UIA_InvokePatternId)
        if not pattern:
            return {"ok": False, "reason": "no_pattern", "pattern": "invoke",
                    "detail": f"GetCurrentPattern(Invoke) hr={hr:#x}", **info}
        try:
            code = _call(pattern, _INVOKE)(ctypes.c_void_p(pattern))
        finally:
            _release(pattern)
        return {"ok": int(code) == S_OK, "pattern": "invoke", "hr": int(code), **info}

    return _submit(_with_element(hwnd, body), timeout_ms)


def set_value(hwnd: int, text: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict:
    """对 ``hwnd`` 调 ValuePattern.SetValue（Edit / ComboBox 的编辑框）。"""

    def body(element: int) -> dict:
        info = _describe(element)
        pattern, hr = _pattern(element, UIA_ValuePatternId)
        if not pattern:
            return {"ok": False, "reason": "no_pattern", "pattern": "value",
                    "detail": f"GetCurrentPattern(Value) hr={hr:#x}", **info}
        try:
            code = _call(pattern, _SET_VALUE, ctypes.c_wchar_p)(ctypes.c_void_p(pattern),
                                                                ctypes.c_wchar_p(text))
        finally:
            _release(pattern)
        return {"ok": int(code) == S_OK, "pattern": "value", "hr": int(code), **info}

    return _submit(_with_element(hwnd, body), timeout_ms)


def probe(hwnd: int, *, timeout_ms: int = PROBE_TIMEOUT_MS) -> dict:
    """只查元素信息与可用 pattern，不动手（诊断 / 选通道用）。"""

    def body(element: int) -> dict:
        info = _describe(element)
        patterns = []
        for pattern_id, name in ((UIA_InvokePatternId, "invoke"),
                                 (UIA_ValuePatternId, "value")):
            pattern, _hr = _pattern(element, pattern_id)
            if pattern:
                patterns.append(name)
                _release(pattern)
        return {"ok": True, "patterns": patterns, **info}

    return _submit(_with_element(hwnd, body), timeout_ms)
