"""托管模式（Host 拥有桌面）与跨进程契约版本 —— 本模块只用标准库。

**为什么单开一个模块**：同一个开关要在两处生效，而它们是两个进程。

  * HTTP 服务进程（`ui_locate.server`）—— 真正的执行体。它抓屏、列窗口、
    切前台、注入输入。托管模式下这些能力必须由它自己关掉：MCP 进程的 env
    约束不了它，调用方也可以绕过 MCP 直接 POST。
  * MCP 进程（`ui_locate.mcp_server`）—— 薄客户端。它同样读这个变量，
    但那是**提前告知**，不是防线。

所以判定只有一个来源：进程自己的环境变量 ``UI_LOCATE_CUA_MANAGED``。不看
MCP 传了什么、不看调用方是谁、不看 runtime.json —— 这三样都能被绕开或过期。

契约握手
--------
``/health`` 会带上 ``contractVersion`` 与 ``managed``。Host 侧在调用前必须
校验这两项：旧版服务没有 ``contractVersion``（它对 ``/locate-image`` 只会
回 404），而未设变量的独立服务会回 ``managed: false``。两者都该被拒绝，
而不是"先试试看" —— 试出来的错误会伪装成"定位不准"。
"""

from __future__ import annotations

import os

# 跨进程契约版本。**只在破坏性变更时加一**：字段增删、坐标语义变化、
# 错误码重命名都算。Host 侧硬校验这个数字，所以加一等于让旧 Host 拒绝新服务
# （这正是想要的方向：宁可握手失败，也不要用错坐标）。
CONTRACT_VERSION = 1

MANAGED_ENV = "UI_LOCATE_CUA_MANAGED"

_TRUE = ("1", "true", "yes", "on")

# 自主桌面能力的路由。托管模式下逐条拒绝，不靠"隐藏工具"。
# 这份清单是**权威**的：server.do_GET / do_POST 的每条分支都必须落在这里
# 或下面的白名单里，tests/test_managed_http.py 会扫源码核对。
DESKTOP_GET_ROUTES = frozenset({
    "/monitors",   # 列显示器
    "/windows",    # 列窗口
    "/frames",     # 帧记忆摘要（回合=抓过屏）
    "/frame",      # 取帧像素
    "/find",       # 抓屏 + 检测
})

DESKTOP_POST_ROUTES = frozenset({
    "/detect",     # 抓屏（或读 path）+ 检测
    "/locate",     # 抓屏 + 找文字
    "/marks",      # 抓屏 + 标注
    "/watch",      # 连续抓屏
    "/diff",       # 抓屏 + 差分
    "/focus",      # 切前台
    "/act",        # 定位 + 注入输入
    "/steps",      # 多步注入输入
})

# 托管模式下仍然可用的路由：给定图片的分析、状态查询，以及"暂停/恢复"这个
# 安全开关本身 —— 暂停是"别动我的桌面"，拒掉它等于不让人刹车。
ALLOWED_GET = frozenset({"/health"})
ALLOWED_POST = frozenset({"/locate-image", "/compare-images", "/pause", "/resume"})

# 托管模式下桌面操作要去**另一个实例**做：独立（standalone）实例的
# `ui_locate_act` + delivery="background"。把被拒的工具名写进 hint 只会让
# 代理换个名字再撞一次同样的墙，所以这里只写真实能用上的入口。
_HOST_TOOLS = "独立（standalone）实例的 ui_locate_act 并传 delivery=\"background\""


def enabled() -> bool:
    """本进程是否处于托管模式。

    每请求读一次环境变量而不是在 import 时缓存：环境在进程生命周期内不变，
    读一次的开销可以忽略；而缓存会让测试无法在不重载模块的前提下切换。
    """
    return os.environ.get(MANAGED_ENV, "").strip().lower() in _TRUE


def refusal(where: str, *, method: str = "", contract: int = CONTRACT_VERSION) -> dict:
    """托管模式下拒绝一次桌面能力调用的结构化回包（HTTP 403 / MCP JSON）。

    ``where`` 是路由或工具名。回包里**不包含任何屏幕内容**——连"为什么拒绝"
    都不该顺带泄露屏幕上有几块屏。
    """
    return {
        "ok": False,
        "code": "CUA_MANAGED",
        "managed": True,
        "contractVersion": contract,
        "path": where,
        "method": method.upper() if method else None,
        # 说清楚"这不是坏了"：能力还在，只是归 Host 管。
        "error": (
            "托管模式：本服务不提供自主桌面能力（抓屏 / 列窗口 / 切前台 / 注入输入）。"
        ),
        "hint": "托管实例不提供桌面能力；桌面操作请用" + _HOST_TOOLS + "；"
                "只分析给定图片请用 /locate-image 或 ui_locate_image；"
                "比较两张给定图片请用 /compare-images。",
    }


def blocked(path: str, method: str) -> dict | None:
    """托管模式下这条路由该不该拒；该拒就回拒绝体，否则 None。

    只对**已知的桌面路由**返回拒绝：未知路径仍然走 404，否则托管模式会把
    拼错的 URL 报成"权限问题"，排查时容易被带偏。
    """
    if not enabled():
        return None
    routes = DESKTOP_GET_ROUTES if method.upper() == "GET" else DESKTOP_POST_ROUTES
    if path in routes:
        return refusal(path, method=method)
    return None


def health_fields() -> dict:
    """``/health`` 里与托管模式有关的那几项。"""
    is_managed = enabled()
    return {
        "contractVersion": CONTRACT_VERSION,
        "managed": is_managed,
        # 能力表：Host 可以据此决定走哪条路，而不必猜。
        "capabilities": {
            # 纯图片分析：给图就能干，两种模式都在。
            "image": True,
            # 逐像素比较。Host 在动作前就要握手问这一项：没有它，只能先点
            # 一下再发现服务没法验证"到底变没变"。
            "compare": True,
            "desktop": not is_managed,
        },
    }
