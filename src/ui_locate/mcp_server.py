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
        #
        # `trust_env=False` 不是可选项。默认 httpx 会读 `HTTP_PROXY` /
        # `ALL_PROXY` 这些环境变量，于是访问 `127.0.0.1:7788` 的请求会被送去
        # 本机代理——探活永远看不到自己的服务，而且代理可能回 503 或干脆挂住。
        # 本机服务不该走代理，这一条与「系统设了代理」无关。
        _client = httpx.Client(base_url=DEFAULT_BASE, timeout=180.0, trust_env=False)
    return _client


def _probe() -> dict[str, Any] | None:
    """服务活着就返回 /health，否则 None。短超时，不要卡住。"""
    try:
        with httpx.Client(timeout=3.0, trust_env=False) as client:
            r = client.get(f"{DEFAULT_BASE}/health")
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


def _spawn_server() -> subprocess.Popen | None:
    """在后台拉起常驻服务（独立进程，本进程退出也不受影响）。

    用 DETACHED_PROCESS 让它脱离 MCP 进程的生命周期：宿主重启 MCP 时
    不该把预热好的服务一起杀掉，否则下次又要等一轮预热（CUDA 约 35 秒）。

    三件事一起做，才能做到「不弹窗、不留 conhost」：
      1. 用 pythonw.exe（Windows 子系统，不分配控制台）；
      2. CREATE_NO_WINDOW（双保险，也给退回 python.exe 时兜底）；
      3. 三路 stdio 一律 DEVNULL —— 只要有可继承的句柄，就还有可能挂上控制台。

    日志写不进去时**不能因此拉不起服务**。曾经就是这里直接 `open()`，在只读
    的运行目录里抛 `PermissionError`，而错误信息只提了一个日志文件名，看起来
    像权限问题而不是「服务没起来」。现在失败就退回 DEVNULL：没有日志总比没有
    定位能力强。
    """
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | 0x08000000  # CREATE_NO_WINDOW
        )

    log_dir = Path(ROOT)
    out = err = subprocess.DEVNULL
    opened: list[Any] = []
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        out = open(log_dir / "server.log", "ab")
        opened.append(out)
        err = open(log_dir / "server.err.log", "ab")
        opened.append(err)
    except OSError:
        for handle in opened:
            try:
                handle.close()
            except OSError:
                pass
        out = err = subprocess.DEVNULL

    cmd = [_console_free_python(), "-m", SERVER_MODULE]
    try:
        return subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(ROOT),
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError:
        return None
    finally:
        for handle in opened:
            try:
                handle.close()
            except OSError:
                pass


def _log_tail(name: str, limit: int = 800) -> str:
    """读日志尾部；读不到就返回空串。"""
    try:
        text = (Path(ROOT) / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:].strip()


def ensure_ready(wait_s: float = 120.0) -> dict[str, Any]:
    """确保服务可用。必要时拉起并等待预热完成。"""
    h = _probe()
    if h and h.get("ready"):
        return h

    child = _spawn_server()

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(1.0)
        h = _probe()
        if h and h.get("ready"):
            return h

        # 进程已经退出就别再等到超时了——多等 120 秒只会让人以为在预热。
        # 典型死因：端口被占、模型路径不对、venv 里缺包。
        if child is not None and child.poll() is not None:
            tail = _log_tail("server.err.log") or _log_tail("server.log")
            raise RuntimeError(
                f"ui-locate 服务启动后退出了（exit={child.returncode}）。"
                + (f"日志尾部：{tail}" if tail else f"日志在 {ROOT / 'server.err.log'}。")
            )

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


def _post_any(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST 并解析 JSON，不看状态码。

    /act 与 /steps 把「指令写错了」报成 400，那是**有效回答**不是故障：
    里面带着「第几步、什么原因」这些模型必须看到的信息。用 _post 会把它
    变成一条 RuntimeError 字符串，结构化信息全丢。
    """
    ensure_ready()
    r = _http().post(path, json=payload)
    try:
        body = r.json()
    except ValueError:
        return {"ok": False, "error": f"/act 返回 {r.status_code}，且不是 JSON：{r.text[:300]}"}
    if not isinstance(body, dict):
        return {"ok": False, "error": f"/act 返回了 {type(body).__name__}，期望对象"}
    body.setdefault("status", r.status_code)
    return body


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
    r = _http().get("/monitors")
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
def ui_locate_act(
    text: str = "",
    action: str = "click",
    x: int | None = None,
    y: int | None = None,
    verify: str = "screen",
    monitor: str = "",
    settle_ms: int = 220,
    count: int = 1,
    button: str = "left",
    delivery: str = "auto",
    to: str = "",
    delta: int = -3,
    value: str = "",
    key: str = "",
    keys: str = "",
    verify_text: str = "",
    verify_timeout_ms: int = 3000,
) -> str:
    """定位 → 动作 → 验证，**一次调用走完**。

    这是「快」的关键：以前是「找坐标」一次往返、「点击」一次往返、「截图确认」
    又一次往返，每次 8~12 秒。现在中间这些步骤都在服务进程里跑，只把结论
    带回来。

    参数：
      text        要操作的目标文字（推荐）。界面一变坐标就变，文字不变。
                  也可以不给 text，改用 x + y 直接指定坐标。
      action      click（默认）/ double_click / right_click / middle_click /
                  hover / drag / scroll / type / press / hotkey / wait
      verify      动作后怎么确认生效：
                    "screen"（默认）—— 点击点周围像素变了没有
                    "text"          —— 指定时间内有没有出现 verify_text
                    "none"          —— 不验证，快，但也就不知道成没成
      monitor     ""（整个虚拟桌面）/ "primary" / "0" / "1" / 设备名
      settle_ms   动作后等多久再验证，默认 220 毫秒（等界面切换动画）
      count       连点次数（action=click 时）
      button      鼠标键 left / right / middle
      delivery    输入通道 send_input（系统队列，要求前台）/ message（投窗口
                  消息，后台可用）/ auto（先前者，光标没动再试后者）
      to          action=drag 的终点：文字或 "x,y"
      delta       action=scroll 的幅度，负数向下
      value       action=type 要输入的文字
      key / keys  action=press 的单键，或 action=hotkey 的组合键（"ctrl+s"）
      verify_text verify="text" 时要等的文字
      verify_timeout_ms  等文字的时限，默认 3000

    返回 JSON：
      ok        流程是否走完（**不代表动作生效了**）
      verified  动作是否被确认生效（true / false / null=没验证）
      target    实际点的位置 {text, cx, cy, w, h, conf}
      verify    验证细节 {kind, changed, changed_enough} 或 {kind, found, ...}
      timings   find_ms / act_ms / verify_ms / total_ms
      stage     失败时在哪一步：find / act / verify

    判读要点：`ok: true, verified: false` 表示「点了但屏幕上没看出变化」——
    目标可能没吃到这次点击，也可能界面本来就不会变（比如点空白处）。
    这种情况换 delivery="message" 或改用 hotkey 更可靠。
    """
    payload: dict[str, Any] = {"action": action, "verify": verify,
                               "settle_ms": settle_ms}
    if text:
        payload["text"] = text
    if x is not None and y is not None:
        payload["x"] = x
        payload["y"] = y
    if monitor:
        payload["monitor"] = monitor
    if count and int(count) != 1:
        payload["count"] = int(count)
    if button:
        payload["button"] = button
    if delivery:
        payload["delivery"] = delivery
    if to:
        payload["to"] = to
    if delta != -3:
        payload["delta"] = int(delta)
    if value:
        payload["value"] = value
    if key:
        payload["key"] = key
    if keys:
        payload["keys"] = keys

    if verify == "text":
        payload["verify"] = {"text": verify_text, "timeout_ms": int(verify_timeout_ms)}
    elif verify in ("none", ""):
        payload["verify"] = None

    return json.dumps(_post_any("/act", payload), ensure_ascii=False)


@mcp.tool()
def ui_locate_steps(steps: str) -> str:
    """顺序执行一串动作，任一步失败即停。

    用来把「打开背包 → 切到材料页 → 找到助剂 → 数量拉到 20 → 确认购买」这种
    流程压成一次工具调用。五步就是五次模型往返，合成一条只剩一次。

    steps 是一个 JSON 数组字符串，每项是一个 `ui_locate_act` 的参数对象，另外
    支持两个额外字段：

      wait_before  动作前先等某段文字出现（比固定 sleep 靠谱）
                   写成 "确认购买" 或 {"text": "...", "timeout_ms": 8000}
      on_fail      "abort"（默认，失败即停）| "continue"（失败继续下一步）
      action:"wait" 时是纯等待步骤：不动手，只等 text 出现

    示例：
      [{"text": "背包", "action": "click", "verify": "text",
        "verify_text": "材料"},
       {"wait_before": "材料", "text": "精锻助剂", "action": "double_click"},
       {"text": "确认购买", "action": "click", "verify": "screen"}]

    返回 JSON：{ok, stage, steps:[每步的结果], completed, failed_index?, timings}
      失败时 failed_index 是**从 0 开始**的序号，对照你传进来的数组即可。
    """
    try:
        parsed = json.loads(steps) if isinstance(steps, str) else steps
    except json.JSONDecodeError as exc:
        return json.dumps({"ok": False, "error": f"steps 不是合法 JSON：{exc}"},
                          ensure_ascii=False)
    if not isinstance(parsed, list) or not parsed:
        return json.dumps({"ok": False, "error": "steps 需要一个非空数组"},
                          ensure_ascii=False)
    return json.dumps(_post_any("/steps", {"steps": parsed}), ensure_ascii=False)


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
