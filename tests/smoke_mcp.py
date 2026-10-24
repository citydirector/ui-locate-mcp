"""MCP 冒烟测试：走真实 stdio 协议，验证握手、工具清单、实际调用。

为什么不用 FastMCP 的 in-process 测试：那样绕过了 stdio 序列化与启动路径，
验证不到「宿主真能把进程拉起来」这件事——而这正是最容易坏的一环。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# 解释器优先级：UI_LOCATE_PYTHON 环境变量 → 包内 .venv → 当前解释器。
# 装到用户级目录后不存在包内 .venv，所以必须能退回 sys.executable。
PY = Path(os.environ.get("UI_LOCATE_PYTHON") or (ROOT / ".venv" / "Scripts" / "python.exe"))
if not PY.is_file():
    PY = Path(sys.executable)


def send(proc: subprocess.Popen, obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    proc.stdin.write(line.encode("utf-8"))
    proc.stdin.flush()


def recv(proc: subprocess.Popen, timeout_s: float = 180.0) -> dict:
    """读一条 JSON-RPC 响应，跳过通知（没有 id 的）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP 进程关闭了 stdout")
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue
        if "id" in msg:
            return msg
    raise TimeoutError(f"{timeout_s}s 内没有收到响应")


def main() -> int:
    # 用 -m 启动，与 bundle 的 cordis.patch.yml 保持一致；源码目录走 PYTHONPATH
    # 是为了让未安装（直接跑仓库）时也能工作。
    print(f"启动 MCP: {PY} -m ui_locate.mcp_server")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.Popen(
        [str(PY), "-m", "ui_locate.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(ROOT),
        env=env,
    )
    assert proc.stdin and proc.stdout
    failures: list[str] = []

    try:
        # 1. 握手
        send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "1"},
            },
        })
        init = recv(proc, 60)
        if "result" in init:
            info = init["result"].get("serverInfo", {})
            print(f"[1] initialize OK  server={info.get('name')} v={info.get('version')}")
        else:
            failures.append(f"initialize 失败: {init}")
            return 1

        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        # 2. 工具清单
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tl = recv(proc, 60)
        tools = [t["name"] for t in tl.get("result", {}).get("tools", [])]
        print(f"[2] tools/list OK  {tools}")
        expected = {"locate_text", "detect_elements", "find_all", "list_monitors", "health"}
        missing = expected - set(tools)
        if missing:
            failures.append(f"缺工具: {missing}")

        # 3. health（不需要模型，最快）
        send(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "health", "arguments": {}},
        })
        r = recv(proc, 60)
        payload = json.loads(r["result"]["content"][0]["text"])
        print(f"[3] health -> ready={payload.get('ready')} provider={payload.get('active')}")

        # 4. 真正调用定位（这一步会等模型，慢）
        t0 = time.monotonic()
        send(proc, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "locate_text",
                "arguments": {"text": "插件", "conf": 0.2},
            },
        })
        r = recv(proc, 180)
        dt = (time.monotonic() - t0) * 1000
        if "error" in r:
            failures.append(f"locate_text 报错: {r['error']}")
        else:
            res = json.loads(r["result"]["content"][0]["text"])
            print(f"[4] locate_text -> ok={res.get('ok')} xy=({res.get('x')},{res.get('y')})"
                  f" matched={res.get('matched')!r}  {dt:.0f}ms")
            if not res.get("ok"):
                failures.append(f"locate_text 未命中: {res}")

        # 5. detect_elements（验证 body 参数真的被吃到）
        t0 = time.monotonic()
        send(proc, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {
                "name": "detect_elements",
                "arguments": {"conf": 0.25, "text_mode": "mosaic", "tiled": True},
            },
        })
        r = recv(proc, 180)
        dt = (time.monotonic() - t0) * 1000
        res = json.loads(r["result"]["content"][0]["text"])
        n = len(res.get("elements", []))
        print(f"[5] detect_elements -> {n} 元素  text_mode={res.get('text_mode')}  {dt:.0f}ms")
        if not n:
            failures.append("detect_elements 返回 0 个元素")
        if res.get("text_mode") != "mosaic":
            failures.append(f"body 参数没吃到，text_mode={res.get('text_mode')}")

        # 6. find_all
        send(proc, {
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "find_all", "arguments": {"text": "插件", "conf": 0.2}},
        })
        r = recv(proc, 180)
        res = json.loads(r["result"]["content"][0]["text"])
        ms = res.get("matches", [])
        print(f"[6] find_all -> {len(ms)} 个候选" +
              (f"，首选 {ms[0]['text']!r} ({ms[0]['cx']},{ms[0]['cy']})" if ms else ""))

        # 7. list_monitors（多显示器几何）—— 顺带确认副屏负坐标被正确表达
        send(proc, {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "list_monitors", "arguments": {}},
        })
        r = recv(proc, 60)
        res = json.loads(r["result"]["content"][0]["text"])
        mons = res.get("monitors", [])
        vs = res.get("virtual", {})
        print(f"[7] list_monitors -> {len(mons)} 块屏  dpi={res.get('dpi_awareness')}"
              f"  virtual={vs.get('w')}x{vs.get('h')}@{vs.get('x')},{vs.get('y')}")
        for m in mons:
            print(f"      [{m['index']}] {m['device']} {m['w']}x{m['h']}"
                  f" at ({m['x']},{m['y']}){' primary' if m['primary'] else ''}")
        if not mons:
            failures.append("list_monitors 返回 0 块屏")

        # 8. detect_elements 指定副屏，验证 origin 偏移真的生效
        if len(mons) > 1:
            sec = mons[1]
            send(proc, {
                "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {
                    "name": "detect_elements",
                    "arguments": {"monitor": str(sec["index"]), "conf": 0.3},
                },
            })
            r = recv(proc, 180)
            res = json.loads(r["result"]["content"][0]["text"])
            els = res.get("elements", [])
            origin = res.get("origin")
            print(f"[8] detect_elements(monitor={sec['index']}) -> {len(els)} 元素"
                  f"  origin={origin}")
            # 副屏原点为负时，元素中心必须落在负坐标区（除非整块屏都没内容）
            if origin and (origin[0] < 0 or origin[1] < 0) and els:
                wrong = [
                    e for e in els
                    if e["cx"] < origin[0] or e["cy"] < origin[1]
                    or e["cx"] > origin[0] + sec["w"] or e["cy"] > origin[1] + sec["h"]
                ]
                if wrong:
                    failures.append(
                        f"副屏 {len(wrong)} 个元素坐标超出该屏范围，origin 偏移可能有误"
                    )
                else:
                    print("      origin 偏移正确：所有元素都在副屏范围内")
            if not els:
                failures.append("副屏 detect_elements 返回 0 个元素")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        if err.strip():
            print("\n--- stderr ---")
            print(err[-2000:])

    print()
    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
