"""ui-locate MCP：把本机视觉定位引擎包装成 MCP 工具。

**架构：MCP 进程是薄客户端，不做推理。**

为什么不在这里直接跑模型：
  实测每个新进程的首次 CUDA 卷积都要 ~35 秒（cuDNN 要为 113 个卷积层逐一
  枚举算法，8GB 卡上绕不过去；DirectML 约 2 秒，但常驻的结构照旧）。MCP
  进程会被宿主反复拉起，如果它自己加载模型，每次握手都要等半分钟——工具
  调用的超时都撑不到。
  所以推理在常驻 HTTP 服务里做一次、一直活着；MCP 进程只管转发，启动是毫秒级。

  服务没起来时，本进程会自动在后台拉起它（首次预热较慢），
  之后的调用就都是几十到几百毫秒。

**边界：只定位，不点击。**
  找到坐标后，调用方用 `cua_driver_native__click(scope="desktop", x=, y=)` 去点。
  理由见 README 的「边界」一节：本机 Python 进程调 SendInput / SetCursorPos
  会静默失败，输入注入这件事有可靠通道，没必要重造。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from ui_locate import config

ROOT = config.state_dir()


DEFAULT_BASE = config.base_url()
SERVER_MODULE = "ui_locate.server"

mcp = FastMCP("ui-locate")

_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        # 超时给足：全屏 full 模式 OCR 要两秒多，首次拉起还要等预热
        _client = httpx.Client(base_url=DEFAULT_BASE, timeout=180.0)
    return _client


def _probe() -> dict[str, Any] | None:
    """服务活着就返回 /health，否则 None。短超时，不要卡住。"""
    try:
        r = httpx.get(f"{DEFAULT_BASE}/health", timeout=3.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _console_free_python() -> str:
    """挑一个不会弹控制台的解释器。

    为什么必须换：`python.exe` 是 **Console 子系统**程序，Windows 启动它时
    一定会分配一个控制台（哪怕 `CREATE_NO_WINDOW`，也只是把窗口藏起来，
    conhost.exe 照样挂上去，进程列表里能看见）。要真正不碰控制台，只能用
    `pythonw.exe` —— 它是 **Windows 子系统**，压根不分配控制台。

    实测本机 venv 里有 pythonw.exe；万一没有（某些精简安装），退回 python.exe，
    此时至少 `CREATE_NO_WINDOW` 能保证窗口不可见，只是会留一个 conhost。
    """
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def _spawn_server() -> None:
    """在后台拉起常驻服务（独立进程，本进程退出也不受影响）。

    用 DETACHED_PROCESS 让它脱离 MCP 进程的生命周期：宿主重启 MCP 时
    不该把预热好的服务一起杀掉，否则下次又要等一轮预热（CUDA 约 35 秒）。

    三件事一起做，才能做到「不弹窗、不留 conhost」：
      1. 用 pythonw.exe（Windows 子系统，不分配控制台）；
      2. CREATE_NO_WINDOW（双保险，也给退回 python.exe 时兜底）；
      3. 三路 stdio 一律 DEVNULL —— 只要有可继承的句柄，就还有可能挂上控制台。
    """
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | 0x08000000  # CREATE_NO_WINDOW
        )

    log_dir = ROOT
    log_dir.mkdir(parents=True, exist_ok=True)

    cmd = [_console_free_python(), "-m", SERVER_MODULE]
    with open(log_dir / "server.log", "ab") as out, open(
        log_dir / "server.err.log", "ab"
    ) as err:
        subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(ROOT),
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )


def ensure_ready(wait_s: float = 120.0) -> dict[str, Any]:
    """确保服务可用。必要时拉起并等待预热完成。"""
    h = _probe()
    if h and h.get("ready"):
        return h

    _spawn_server()

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(1.0)
        h = _probe()
        if h and h.get("ready"):
            return h

    raise RuntimeError(
        f"ui-locate 服务在 {wait_s:.0f}s 内没有就绪（{DEFAULT_BASE}）。"
        "首次启动要预热模型（CUDA 约 35 秒，DirectML 约 2 秒）；若持续失败，看 "
        f"{ROOT / 'server.err.log'}。"
    )


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_ready()
    r = _http().post(path, json=payload)
    if r.status_code != 200:
        raise RuntimeError(f"{path} 返回 {r.status_code}: {r.text[:400]}")
    return r.json()


# ── 工具 ────────────────────────────────────────────────────────────────────


@mcp.tool()
def locate_text(
    text: str,
    conf: float = 0.25,
    text_mode: str = "mosaic",
    monitor: str = "",
    smart: bool = True,
) -> str:
    """按屏幕上的文字找到它的精确像素坐标。

    这是最常用的工具：给一段界面文字（按钮名、菜单项、标题），返回它的中心坐标，
    调用方拿坐标去点。坐标是算出来的，不是估的。

    参数：
      text       要查找的文字，如 "保存"、"编辑"、"插件"
      conf       检测置信度下限，默认 0.25；找不到时降到 0.15 再试
      text_mode  "mosaic"（默认，只认控件区域，快 2~4 倍）或
                 "full"（整页 OCR，连正文段落也能认，慢）
      monitor    找哪块屏：""（默认，整个虚拟桌面）/ "primary" / "0" / "1" /
                 设备名如 "\\\\.\\DISPLAY2"
      smart      内容感知切块（对齐切缝、跳过空白块），默认 True

    返回 JSON：{ok, matched, score, x, y, w, h, elements, origin}
      x / y 已经是**屏幕绝对坐标**，可直接交给
      `cua_driver_native__click(scope="desktop", x=x, y=y)`。
      副屏原点为负时也已算进去，调用方不必再换算。

    排序规则：完全相等 > 以查询词开头/结尾 > 包含；同档内「文字长度越接近
    查询词」越优先，然后才看置信度。控件名天然短，长度比 conf 可靠。
    """
    payload: dict[str, Any] = {
        "text": text,
        "conf": conf,
        "text_mode": text_mode,
        "smart": smart,
    }
    if monitor:
        payload["monitor"] = monitor
    res = _post("/locate", payload)
    return json.dumps(res, ensure_ascii=False)


@mcp.tool()
def detect_elements(
    conf: float = 0.3,
    text_mode: str = "mosaic",
    tiled: bool = True,
    monitor: str = "",
    smart: bool = True,
) -> str:
    """抓当前屏幕，列出所有可点元素的坐标与文字。

    用于「我不知道目标叫什么」的场合：先看一眼屏幕上有什么。
    结果里带文字的排在前面。

    参数：
      conf       置信度下限，默认 0.3
      text_mode  "mosaic"（默认，快）或 "full"（认得更全，慢）
      tiled      分块检测，能找回小图标，默认 True
      monitor    ""（整个虚拟桌面）/ "primary" / "0" / "1" / 设备名
      smart      内容感知切块，默认 True

    返回 JSON：{size, origin, elements:[{cx, cy, w, h, conf, text}],
              detect_ms, ocr_ms, tiles}
      cx / cy 是屏幕绝对坐标，origin 是这张图左上角的屏幕坐标。
    """
    payload: dict[str, Any] = {
        "conf": conf,
        "text_mode": text_mode,
        "tiled": tiled,
        "smart": smart,
    }
    if monitor:
        payload["monitor"] = monitor
    res = _post("/detect", payload)
    for it in res.get("elements", []):
        for k in ("cx", "cy", "w", "h", "conf"):
            if k in it:
                it[k] = round(float(it[k]), 1)
    return json.dumps(res, ensure_ascii=False)


@mcp.tool()
def list_monitors() -> str:
    """列出所有显示器及其坐标几何。

    多显示器场景下先调这个，确认要操作的是哪一块、坐标原点在哪。
    副屏原点可能是负数（主屏在右、副屏在左时的典型布局），这直接影响
    坐标换算 —— 返回的 x/y 已把 origin 算进去，是屏幕绝对坐标。

    返回 JSON：{dpi_awareness, virtual:{x,y,w,h}, monitors:[{index,device,
              primary,x,y,w,h}]}
      index 可直接作为其它工具的 monitor 参数（"0"、"1"）。
    """
    r = httpx.get(f"{DEFAULT_BASE}/monitors", timeout=10.0)
    if r.status_code != 200:
        raise RuntimeError(f"/monitors 返回 {r.status_code}: {r.text[:300]}")
    return json.dumps(r.json(), ensure_ascii=False)


@mcp.tool()
def find_all(
    text: str,
    img_path: str = "",
    conf: float = 0.25,
    text_mode: str = "mosaic",
    monitor: str = "",
    smart: bool = True,
) -> str:
    """找出所有匹配该文字的元素，按匹配度排序（不止第一个）。

    当一个词在屏幕上出现多次（比如侧栏和正文都有「插件」），用这个看全部候选，
    自己挑一个。`locate_text` 只给最匹配的那一个。

    参数：
      text       要查找的文字
      img_path   可选，指定图片路径；留空则抓当前屏幕
      conf       置信度下限
      text_mode  "mosaic" 或 "full"
      monitor    找哪块屏（img_path 给了就忽略）
      smart      内容感知切块

    返回 JSON：{query, matches:[{text, cx, cy, w, h, conf}], origin}
    """
    payload: dict[str, Any] = {
        "conf": conf,
        "text_mode": text_mode,
        "text": True,
        "tiled": True,
        "smart": smart,
    }
    if img_path:
        payload["path"] = img_path
    elif monitor:
        payload["monitor"] = monitor
    res = _post("/detect", payload)

    q = text.strip().lower()
    hits: list[dict[str, Any]] = []
    for it in res.get("elements", []):
        t = (it.get("text") or "").strip().lower()
        if not t:
            continue
        if t == q:
            tier = 0
        elif t.startswith(q) or t.endswith(q):
            tier = 1
        elif q in t or t in q:
            tier = 2
        else:
            continue
        hits.append(
            {
                "text": it.get("text"),
                "cx": round(float(it["cx"]), 1),
                "cy": round(float(it["cy"]), 1),
                "w": round(float(it["w"]), 1),
                "h": round(float(it["h"]), 1),
                "conf": round(float(it.get("conf", 0)), 2),
                "_rank": (tier, abs(len(t) - len(q)), -float(it.get("conf", 0))),
            }
        )
    hits.sort(key=lambda d: d["_rank"])
    for h in hits:
        h.pop("_rank", None)
    return json.dumps({"query": text, "matches": hits}, ensure_ascii=False)


@mcp.tool()
def health() -> str:
    """查看定位服务状态：是否就绪、用的哪个推理后端、预热花了多久。"""
    h = _probe()
    if not h:
        return json.dumps(
            {
                "ok": False,
                "ready": False,
                "hint": "服务未运行。调用任意其它工具会自动拉起（CUDA 首次约 35 秒，DirectML 约 2 秒）。",
            },
            ensure_ascii=False,
        )
    return json.dumps(h, ensure_ascii=False)


def main() -> None:
    """stdio 传输入口。`python -m ui_locate.mcp_server` 与 console script 都走这里。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
