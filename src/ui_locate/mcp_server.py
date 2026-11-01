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

**边界：能动手，但不保证动手生效。**
  想只拿坐标，用 `locate_text` / `detect_elements` / `find_all`，再自己调
  `cua_driver_native__click(scope="desktop", x=, y=)`。
  想让一次调用把「找 → 做 → 验」走完，用 `ui_locate_act` / `ui_locate_steps`。
  后者会回带独立的 `verified`：`true` 才算看到效果，`false` 是确实没变化，
  `null` 是**没法判断**（抓屏被拦）。判成败看 `verified`，不要只看 `ok`。
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
    # 必须显式 ensure_ready：`health` 之外，这里是唯一走 GET 的工具，以前
    # 直接 `_http().get(...)`，于是成了唯一不会自动拉起服务的工具。表现很
    # 像「服务坏了」——第一次调用它的人（包括我自己）看到的是
    # `[WinError 10061] 连接被拒`，而紧接着别的工具却都正常。
    # 看状态不该有副作用，所以 `health` 仍然只用 _probe()。
    ensure_ready()
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
def ui_locate_marks(
    monitor: str = "primary",
    window: str = "",
    box: str = "",
    conf: float = 0.2,
    text: bool = True,
    max_marks: int = 200,
    max_side: int = 1600,
    grid: int = 0,
    cells: bool = True,
    incremental: bool = True,
):
    """截一张屏，把每个可点的东西都标上编号 —— **直接返回图片**。看图选号，再用
    `ui_locate_act(mark=编号)` 去点。不用自己估坐标。

    什么时候用它：目标没有稳定文字（游戏图标、地图上的单位、工具栏小图标），
    或者你看得出「是哪个」却说不准「在哪个像素」。坐标由检测器算，像素级；
    你只负责认。

    小按钮看不清、编号挤成一团时，用 `box` 只看一块：那块会先放大再检测、
    标注图也跟着放大（最多 4 倍），16px 的按钮也能一个个分开编号。

    **要的东西没有框**（游戏物品格、地图单位、自绘控件常见）：给 `grid`，
    图上会再叠一层带名字的网格（列 A、B…，行 1、2…）。两轮到像素级：
      1. `ui_locate_marks(grid=8)` → 看出目标在 "D3"
      2. `ui_locate_marks(box="D3", grid=4)` → 那一格放大重标；有编号就用编号，
         没有就再选一格
      3. `ui_locate_act(mark=…)` 或 `ui_locate_act(cell="B2")`（点格子中心）

    参数：
      monitor    哪块屏："primary"（默认）/ "0" / "1" / 设备名 / "all"
      window     只看一个窗口：标题（或其中一段）/ 句柄。**游戏被别的窗口挡住也能抓**
                 （PrintWindow；抓黑了自动改按屏裁）。给了就忽略 monitor；之后用这张图
                 的编号去点时，会自动先把这个窗口切到前台。窗口列表见 `ui_locate_windows`
      box        只看这块："x1,y1,x2,y2"（**屏幕绝对坐标**），或上一张图的网格
                 名如 "D3"。留空 = 整块屏。
      conf       检测置信度下限，默认 0.2（偏低，宁可多标几个）
      text       顺带 OCR 认字，默认 True；只关心图标时关掉能快 0.5 秒左右
      max_marks  最多标多少个，默认 200
      max_side   标注图长边像素，默认 1600
      incremental 默认 True：和上一次标注同一块屏 / 窗口时先做差分，没变就直接复用、
                 只变了一小块就只对那一块重新检测 + OCR（返回里 incremental.mode =
                 reuse / partial / full）。编号仍然重新分配，旧 mark_set 照样作废
      cells      补「成排成列的同尺寸格子」（物品栏、标签页、技能栏、棋盘、表格），
                 不靠模型，跨游戏通用；默认 True。返回里这类编号 kind="cell"
      grid       叠加网格，长边切几格（短边按比例），0 = 不画（默认）。8 左右合适

    返回：一段 JSON + 一张标注图。
      JSON：{mark_set, count, truncated, candidates, kinds, marks:[{id, text?, cx, cy, w, h}],
             origin, box, scale, timings}
             truncated=true 表示候选比 max_marks 多、被截掉了（candidates 是截前总数）：
             要的东西可能没标上，用 box 只看那一块
      图上每个框左上角（放不下时在右上 / 下方 / 框内）有编号标签，颜色与
      框一致。

    编号只对**这一张图**有效。之后点的时候可以带上 `mark_set`：画面已经被
    重新标注过时会拒绝执行，而不是点到新画面上的另一个同号元素。
    要的东西没被框出来：换 `box` 放大那一块再标一次；还是没有，就说明检测器
    认不出它，改用坐标。
    """
    payload: dict[str, Any] = {
        "monitor": _window_spec(window) or monitor or "primary",
        "conf": conf,
        "text": bool(text),
        "max_marks": int(max_marks),
        "max_side": int(max_side),
    }
    if box:
        payload["box"] = box
    if grid:
        payload["grid"] = int(grid)
    if not cells:
        payload["cells"] = False
    if not incremental:
        payload["incremental"] = False
    res = _post_any("/marks", payload)
    if not res.get("ok"):
        return json.dumps(res, ensure_ascii=False)

    import base64

    from mcp.server.fastmcp.utilities.types import Image

    data = base64.b64decode(res.pop("image_b64"))
    fmt = res.pop("image_format", "jpeg")
    res.pop("status", None)
    return [json.dumps(res, ensure_ascii=False), Image(data=data, format=fmt)]


@mcp.tool()
def ui_locate_act(
    text: str = "",
    mark: int = 0,
    cell: str = "",
    mark_set: int = 0,
    action: str = "click",
    x: int | None = None,
    y: int | None = None,
    verify: str = "screen",
    monitor: str = "",
    window: str = "",
    focus: str = "auto",
    settle_ms: int = 0,
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
    pointer: str = "",
    keys_mode: str = "",
    duration_ms: int = 0,
    watch_ms: int = 0,
):
    """定位 → 动作 → 验证，**一次调用走完**。

    这是「快」的关键：以前是「找坐标」一次往返、「点击」一次往返、「截图确认」
    又一次往返，每次 8~12 秒。现在中间这些步骤都在服务进程里跑，只把结论
    带回来。

    参数：
      text        要操作的**目标**文字（推荐）。界面一变坐标就变，文字不变。
                  ⚠ text 永远是「点哪儿」，不是「输入什么」：action=type 要输入的内容
                  写在 value。{"action":"type","text":"abc"} 的意思是「找到屏幕上叫 abc
                  的东西，再往里打 value」
                  也可以不给 text，改用 mark（编号）或 x + y（坐标）。
                  纯键盘动作（press / hold / hotkey / type）可以什么目标都不给，
                  此时 verify="screen" 改为整屏比对
      mark        `ui_locate_marks` 标注图上的编号。没有稳定文字的目标用它，
                  比自己估坐标准得多。
      cell        标注图上的网格名（如 "B2"），点那一格的中心。格子越小越准：
                  先用 box=格子名 放大重标，再选更小的格子或编号
      mark_set    可选，标注时返回的集合号；画面已被重新标注过就拒绝执行
      action      click（默认）/ double_click / right_click / middle_click /
                  hover / drag / scroll / type / press / hold / hotkey / wait
                  wait 不给 text = 等画面静止（加载完、动画放完），时限 verify_timeout_ms
                  hold = 按住 key/keys 一段时间（游戏里走路、蓄力），时长见 duration_ms
      verify      动作后怎么确认生效：
                    "screen"（默认）—— 点击点周围像素变了没有
                    "text"          —— 指定时间内有没有出现 verify_text
                    "none"          —— 不验证，快，但也就不知道成没成
      monitor     ""（整个虚拟桌面）/ "primary" / "0" / "1" / 设备名
      window      目标窗口：标题（或其中一段）/ 句柄。动手前先把它**切到前台**
                  （SendInput 只送给前台窗口；模型所在的聊天窗口在前台时，按键会打进
                  聊天框），验证时也只抓这个窗口（被挡住也能抓）。纯键盘动作强烈建议给
      focus       "auto"（默认：有 window 用它，否则切到点击点下面的那个窗口）/
                  "off"（不碰前台）。结果里 performed.focus 报告切没切成功
      settle_ms   0（默认）= 自动：verify="screen" 时**等画面停下来**再比对（最多 1.5 秒，
                  结果里 settle.stable / elapsed_ms），其它情况固定等 220ms。
                  给正数 = 固定等这么多毫秒
      count       连点次数（action=click 时）
      button      鼠标键 left / right / middle
      delivery    输入通道 send_input（系统队列，要求前台）/ message（投窗口
                  消息，后台可用）/ auto（先前者，光标没动再试后者）
      to          action=drag 的终点：文字、"x,y" 或编号 "#17"
      delta       action=scroll 的幅度，负数向下
      value       action=type 要输入的文字（不是 text！）
      key / keys  action=press 的单键，或 action=hotkey 的组合键（"ctrl+s"）
      verify_text verify="text" 时要等的文字
      verify_timeout_ms  等文字的时限，默认 3000
      pointer     鼠标怎么动：absolute（默认，一步到位）/ relative（像真鼠标那样推过去）。
                  **全屏游戏点了没反应、光标不动时换 relative** —— 用 Raw Input 的游戏
                  只认相对位移。结果里 relative.tracking=false 表示游戏锁住了系统光标，
                  位置是按 1:1 估算的，务必看 verify 或重新截图确认
      结果里出现 performed.uipi.blocked=true / warning：目标窗口以管理员运行而本服务
                  不是，键鼠会被系统**静默丢弃**（SendInput 照样回「送出了」）。以管理员
                  身份启动 DSH 再试
      keys_mode   按键怎么送：vk（默认）/ scancode（DirectInput 游戏按键没反应时用）
      duration_ms action=hold 按住多久（毫秒，默认 500）；action=press 时表示按住多久再松
      watch_ms    动作之后接着**看一小段**（毫秒，0=不看）：返回里多一个 watch 和一张
                  关键帧拼图（同 ui_locate_watch），用来看结果怎么演变 —— 角色走到哪、
                  弹窗出没出来、技能是否进了冷却。「看 → 做 → 看」一次调用走完

    返回 JSON：
      ok        流程是否走完（**不代表动作生效了**）
      verified  动作是否被确认生效（true / false / null=没验证）
      target    实际点的位置 {text, cx, cy, w, h, conf}
      verify    验证细节 {kind, changed, changed_enough, by, regions} 或 {kind, found, ...}
                regions = 动作前后变了的区域（屏幕坐标，按大小排序）；by="region" 表示
                平均差不够但点击点旁边有一块明显变化（小按钮变色常见）。
                动作前会先静置量一下噪声（约 80ms）：noise.animated=true 表示画面自己
                就在动（水面、粒子、视频），此时阈值按噪声抬高、一直在动的格子不计
      timings   find_ms / act_ms / verify_ms / total_ms
      stage     失败时在哪一步：find / act / verify

    判读要点：`ok: true, verified: false` 表示「点了但屏幕上没看出变化」——
    目标可能没吃到这次点击，也可能界面本来就不会变（比如点空白处）。
    这种情况换 delivery="message" 或改用 hotkey 更可靠。
    """
    payload: dict[str, Any] = {"action": action, "verify": verify}
    if settle_ms and int(settle_ms) > 0:
        payload["settle_ms"] = int(settle_ms)
    if window:
        payload["window"] = window
    if focus and focus != "auto":
        payload["focus"] = False if focus in ("off", "false", "no", "none") else focus
    if action == "wait" and not text:
        payload["timeout_ms"] = int(verify_timeout_ms)
    if text:
        payload["text"] = text
    if mark:
        payload["mark"] = int(mark)
    elif cell:
        payload["cell"] = cell
    if (mark or cell) and mark_set:
        payload["mark_set"] = int(mark_set)
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
    if pointer:
        payload["pointer"] = pointer
    if keys_mode:
        payload["keys_mode"] = keys_mode
    if duration_ms:
        payload["duration_ms" if action in ("hold", "hold_key", "drag") else "hold_ms"] = int(duration_ms)

    if verify == "text":
        payload["verify"] = {"text": verify_text, "timeout_ms": int(verify_timeout_ms)}
    elif verify in ("none", ""):
        payload["verify"] = None

    if watch_ms:
        payload["watch_ms"] = int(watch_ms)
    res = _post_any("/act", payload)
    watch = res.get("watch") if isinstance(res, dict) else None
    if isinstance(watch, dict) and watch.get("image_b64"):
        import base64

        from mcp.server.fastmcp.utilities.types import Image

        data = base64.b64decode(watch.pop("image_b64"))
        fmt = watch.pop("image_format", "jpeg")
        return [json.dumps(res, ensure_ascii=False), Image(data=data, format=fmt)]
    return json.dumps(res, ensure_ascii=False)


def _window_spec(window: str) -> str:
    if not window:
        return ""
    w = str(window).strip()
    low = w.lower()
    if low.startswith(("window:", "hwnd:")):
        return w
    if w.isdecimal() or low.startswith("0x"):
        return f"hwnd:{w}"
    return f"window:{w}"


@mcp.tool()
def ui_locate_windows(title: str = "") -> str:
    """列出可见的顶层窗口（标题、句柄、客户区位置、是否在前台）。

    拿到的标题或句柄可以给 `ui_locate_marks(window=…)` / `ui_locate_act(window=…)` /
    `ui_locate_diff(window=…)`；其它工具的 monitor 参数也接受 "window:标题" / "hwnd:句柄"。

    参数：title  只列标题里含这段文字的窗口（不分大小写），留空 = 全部
    返回 JSON：{count, windows:[{hwnd, title, class, pid, x, y, w, h, foreground, minimized}]}
    """
    ensure_ready()
    params = {"title": title} if title else {}
    r = _http().get("/windows", params=params)
    return json.dumps(r.json(), ensure_ascii=False)


@mcp.tool()
def ui_locate_diff(monitor: str = "primary", window: str = "", cell: int = 32,
                   reset: bool = False) -> str:
    """上次调用之后，屏幕**哪里变了** —— 回几个矩形，不回图，不花视觉 token（几十毫秒）。

    用法：做完一件事（或等了一会儿）后调一次，看变化落在哪：弹窗出现在哪、哪个格子
    亮了、角色走到哪。只在有变化、而且需要看清是什么时，再用 `ui_locate_marks(box=…)`
    放大那一块。第一次调用（或 reset=true）只建立基准帧。

    参数：
      monitor  哪块屏，默认 "primary"；也可 "window:标题"
      window   只看一个窗口（标题或句柄），给了就忽略 monitor
      cell     网格边长（像素），默认 32；越小越细，噪声也越多
      reset    true = 丢掉旧基准，从现在重新开始
    返回 JSON：{first, since_ms, changed_cells, total_cells, changed_frac,
               regions:[{x, y, w, h, cx, cy, cells, strength}], grab_ms, diff_ms}
      regions 是屏幕绝对坐标，按大小排序；strength 是格内平均像素差（0~255，>15 是明显变化）
    """
    payload: dict[str, Any] = {"monitor": _window_spec(window) or monitor or "primary",
                               "cell": int(cell)}
    if reset:
        payload["reset"] = True
    return json.dumps(_post_any("/diff", payload), ensure_ascii=False)


def _with_image(res: dict) -> list:
    """把服务返回里的 image_b64 拆成一张 MCP 图片，其余留在 JSON 里。"""
    import base64

    from mcp.server.fastmcp.utilities.types import Image

    data = base64.b64decode(res.pop("image_b64", "") or b"")
    fmt = res.pop("image_format", "jpeg")
    out: list = [json.dumps(res, ensure_ascii=False)]
    if data:
        out.append(Image(data=data, format=fmt))
    return out


@mcp.tool()
def ui_locate_watch(monitor: str = "primary", window: str = "", duration_ms: int = 3000,
                    fps: float = 5.0, mode: str = "regions", max_keyframes: int = 6):
    """**录几秒看一看**：连抓一小段，挑出关键帧，只把「变了的地方」拼成一张图返回。

    不知道一个操作会引起什么（地图会不会动、怪往哪走、动画什么时候结束、按钮
    是不是在冷却）时，先看几秒再决定；动手之后也可以再看几秒确认结果。比连发
    十几张截图便宜得多：静止画面只回一帧，变化只回裁片。

    水面、粒子、时钟这类**一直在动**的格子会被识别成环境噪声（返回里的 ambient），
    不算关键帧。

    参数：
      monitor       哪块屏，默认 "primary"
      window        只看一个窗口（标题或句柄），被挡住也能抓；给了就忽略 monitor
      duration_ms   录多久，默认 3000，最多 15000
      fps           每秒几帧，默认 5，最多 15
      mode          "regions"（默认）= 第一帧全景 + 之后每个关键帧只贴变化区域裁片；
                    "full" = 每个关键帧整帧缩略图
      max_keyframes 最多几个关键帧（含第一帧），默认 6；多了按变化量挑，保留最后一帧

    返回：JSON + 一张拼图。
      JSON：{static, frames, fps, keyframes:[{index, t_ms, changed_frac,
             regions:[{x, y, w, h, cx, cy, cells, strength}]}], ambient, timings}
      拼图里每块标着 K<序号> t=<秒>，裁片还标着屏幕坐标 @x,y 和尺寸。
      static=true 表示这段时间画面没变（只有第一帧）。
    """
    payload: dict[str, Any] = {"monitor": _window_spec(window) or monitor or "primary",
                               "duration_ms": int(duration_ms), "fps": float(fps),
                               "mode": mode, "max_keyframes": int(max_keyframes)}
    res = _post_any("/watch", payload)
    if not res.get("ok"):
        return json.dumps(res, ensure_ascii=False)
    return _with_image(res)


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
      action:"wait" 时是纯等待步骤：不动手，只等 text 出现；不给 text 就等画面静止
      mark         标注图上的编号（同 ui_locate_act 的 mark）。整串步骤里的编号都
                   指**调用时**最新那张标注图；中途画面变了，后面的编号可能已不准，
                   这种流程请拆开调用、每次重新标注。

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


@mcp.tool()
def ui_locate_frames(limit: int = 10) -> str:
    """列出帧记忆里还剩哪些回合，供 `ui_locate_frame` 取用。

    每次 `ui_locate_act` / `ui_locate_steps` 算一个回合，回合里第 0 帧是
    「动作之前」、第 1 帧是「动作之后」（再往后是 steps 中途多抓的）。
    回合号单调递增、永不复用，所以「第 7 回合」永远指同一段画面。

    为什么要先列一遍：服务只保留最近的若干回合（默认约 256MB，约 16 帧
    2880x1800），更老的会被淘汰。拿一个已经被淘汰的回合号去取只会得到
    「没有这一帧」，所以先看清楚有什么。

    这里**只报摘要不报像素** —— 摘要本身不该变成新的上下文负担。

    参数：
      limit  最多列几个回合（从最新往回数），默认 10。

    返回 {ok, turns: [{turn, frames: [{turn, frame, label, size, origin, age_s}], age_s, bytes}], stats}。
    """
    try:
        r = _http().get("/frames", params={"limit": int(limit)})
    except TypeError:
        return json.dumps({"ok": False, "error": "limit 必须是整数"}, ensure_ascii=False)
    if r.status_code != 200:
        return json.dumps(
            {"ok": False, "error": f"/frames 返回 {r.status_code}: {r.text[:300]}"},
            ensure_ascii=False,
        )
    body = r.json()
    return json.dumps({"ok": True, **body}, ensure_ascii=False)


@mcp.tool()
def ui_locate_frame(
    turn: int = -1,
    frame: int = -1,
    box: str = "",
    scale: float = 0.0,
):
    """从帧记忆里取一帧，或它的一块放大图 —— **直接返回图片**。

    这是 VISTA 那套「无损视觉记忆 + 主动检查」在本地版的落地：
      · **跨时间**：`turn` 指回任意一个还留着的回合，看那一刻的画面。
        典型用法是「刚才点下去以后到底变成什么样了」，或者「三回合前那个
        按钮在不在」。
      · **跨空间**：`box` 指定一块矩形放大看细节。整屏缩略图里 10px 的字
        根本认不出，放大 4 倍就清楚了 —— 但只在需要时才付这份 token。

    为什么值得单独存在：检测/OCR 给的是**结论**（「按钮在 147,170」），
    而结论在生成的那一刻就已经替未来的你判断过「哪些像素以后有用」了。
    这里存的是**原始像素**，什么时候看、看哪一块，由你决定。

    `box` 用**屏幕绝对坐标** x1,y1,x2,y2（副屏原点可能是负数，照样给绝
    对值）。不给 box 就是整帧。自动缩放会把长边拉到 1600px 左右，最多放大
    6 倍（再大只是插值出的糊图）。

    参数：
      turn   回合号。默认 -1 = 最新回合。
      frame  回合内序号。默认 -1 = 该回合最后一帧。
      box    "x1,y1,x2,y2"，屏幕绝对坐标。留空 = 整帧。
      scale  放大倍数。0 = 自动（长边拉到 1600，最多 6 倍）。

    返回：一张 PNG 图片 + 一段 JSON 元信息（含 `box`：这块裁剪在屏幕上的
    位置，据此把图里的坐标换算回屏幕坐标）。
    """
    params: dict[str, Any] = {"turn": int(turn), "frame": int(frame)}
    if box:
        params["box"] = box
    if scale and scale > 0:
        params["scale"] = float(scale)

    ensure_ready()
    r = _http().get("/frame", params=params)
    if r.status_code != 200:
        try:
            return json.dumps({"ok": False, **r.json()}, ensure_ascii=False)
        except ValueError:
            return json.dumps(
                {"ok": False, "error": f"/frame 返回 {r.status_code}: {r.text[:300]}"},
                ensure_ascii=False,
            )

    from mcp.server.fastmcp.utilities.types import Image

    raw_box = r.headers.get("X-Frame-Box") or ""
    meta = {
        "ok": True,
        "turn": r.headers.get("X-Frame-Turn"),
        "frame": r.headers.get("X-Frame-Index"),
        # 这块裁剪对应的屏幕范围。图里看到的相对位置必须加上它才能变成
        # 能交给点击的绝对坐标。
        "box": [int(float(v)) for v in raw_box.split(",") if v] if raw_box else [],
        "scale": r.headers.get("X-Frame-Scale"),
        "bytes": len(r.content),
    }
    return [
        json.dumps(meta, ensure_ascii=False),
        Image(data=r.content, format="png"),
    ]


def main() -> None:
    """stdio 传输入口。`python -m ui_locate.mcp_server` 与 console script 都走这里。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
