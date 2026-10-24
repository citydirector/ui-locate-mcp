"""bundle 结构自检：不依赖 DSH 运行时，只按官方规则逐条核对。

官方规则来自 app.asar 里的 plugin manager 实现：
  - package.json 必须有 dsh.bundle.patch，指向包内一份存在的文件
  - exports 要暴露 ./locale/*.json 与 ./icon
  - 图标必须留在包内，≤256 KiB
  - locale/*.json 要能解析出 meta.title / meta.description
  - cordis.patch.yml 里的 MCP 条目：serverName 匹配 ^[A-Za-z0-9_-]{1,32}$，
    stdio 的 command 必须是绝对路径（!!js 表达式在加载时才求值，
    这里只能检查它确实是 !!js 节点）
"""

import json
import re
import sys
from pathlib import Path

# 默认检查本文件所在测试目录的上一级，也就是包根。
# 不给参数就查当前仓库，给参数就查别的目录（例如装好的副本）。
PKG = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1])
errs: list[str] = []
warns: list[str] = []


def ok(msg: str) -> None:
    print(f"  OK   {msg}")


def bad(msg: str) -> None:
    errs.append(msg)
    print(f"  FAIL {msg}")


def warn(msg: str) -> None:
    warns.append(msg)
    print(f"  WARN {msg}")


print(f"包目录: {PKG}\n")

# ── package.json ────────────────────────────────────────────────────────────
print("[1] package.json")
pkg_path = PKG / "package.json"
if not pkg_path.is_file():
    bad("package.json 不存在")
    raise SystemExit(1)
pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
ok(f"name={pkg.get('name')} version={pkg.get('version')}")

patch_rel = (pkg.get("dsh") or {}).get("bundle", {}).get("patch")
if not patch_rel:
    bad("缺少 dsh.bundle.patch")
elif not (PKG / patch_rel).is_file():
    bad(f"dsh.bundle.patch 指向的文件不存在: {patch_rel}")
else:
    ok(f"dsh.bundle.patch -> {patch_rel}")

exports = pkg.get("exports") or {}
if "./locale/*.json" not in exports:
    warn("exports 未暴露 ./locale/*.json，插件管理器读不到标题描述")
else:
    ok("exports 暴露 ./locale/*.json")
if "./icon" not in exports and "icon" not in pkg:
    warn("既没有 exports['./icon'] 也没有顶层 icon")

# ── 图标 ────────────────────────────────────────────────────────────────────
print("\n[2] 图标")
icon_file = next((PKG / n for n in ("icon.svg", "icon.png", "icon.jpg", "icon.webp") if (PKG / n).is_file()), None)
if icon_file is None:
    warn("包内没有图标文件")
else:
    size = icon_file.stat().st_size
    if size > 256 * 1024:
        bad(f"{icon_file.name} 超过 256 KiB 上限（{size} 字节）")
    else:
        ok(f"{icon_file.name} {size} 字节（≤256 KiB）")

# ── locale ──────────────────────────────────────────────────────────────────
print("\n[3] locale")
loc_dir = PKG / "locale"
found_locale = False
if loc_dir.is_dir():
    for f in sorted(loc_dir.glob("*.json")):
        try:
            meta = json.loads(f.read_text(encoding="utf-8")).get("meta") or {}
        except json.JSONDecodeError as e:
            bad(f"{f.name} 不是合法 JSON: {e}")
            continue
        if not meta.get("title"):
            bad(f"{f.name} 缺少 meta.title")
        else:
            ok(f"{f.name}: {meta['title']}")
            found_locale = True
if not found_locale:
    warn("没有任何可用的 locale 元数据，插件列表里会显示包名")

# ── cordis.patch.yml ────────────────────────────────────────────────────────
print("\n[4] cordis.patch.yml")
patch_text = (PKG / patch_rel).read_text(encoding="utf-8")
if "- insert:" not in patch_text:
    bad("patch 里没有 - insert: 块")
else:
    ok("有 - insert: 块")

m = re.search(r"serverName:\s*(\S+)", patch_text)
if not m:
    bad("缺少 serverName")
elif not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", m.group(1)):
    bad(f"serverName 不合规（^[A-Za-z0-9_-]{{1,32}}$）: {m.group(1)}")
else:
    ok(f"serverName={m.group(1)} 合规")

if "transport: stdio" in patch_text:
    ok("transport: stdio")
    cmd_line = next((ln for ln in patch_text.splitlines() if "command:" in ln), "")
    if "!!js" in cmd_line:
        ok("command 是 !!js 表达式（加载时求值成绝对路径）")
        if "isAbsolute" not in patch_text and "process.env" not in cmd_line:
            warn("!!js 表达式既没读环境变量也没拼绝对路径，可能不满足绝对路径要求")
    elif re.search(r"command:\s*['\"]?[A-Za-z]:[\\/]", cmd_line):
        ok("command 是 Windows 绝对路径")
    else:
        bad(f"stdio 的 command 必须是绝对路径，当前: {cmd_line.strip()}")
else:
    warn("不是 stdio 传输，跳过 command 检查")

# ── Host 插件行（可选，加了就必须合规）──────────────────────────────────────
# 官方规则：patch 里插一行自己的插件时，`name` 要写**包名**，不能写 `./x.mjs`
# 这样的相对路径。DSH 会把相对名解析成绝对 file URL，配置编辑器首次保存就
# 把它写进用户 patch，便携版一搬目录就断。
print("\n[4b] Host 插件行")
rows = re.findall(r"-\s+id:\s*(\S+)\s*\n\s+name:\s*['\"]?([^'\"\n]+)['\"]?", patch_text)
own_rows = [r for r in rows if r[1].strip() != "@deepseek-ai/dsh-mcp-client"]
if not own_rows:
    warn("patch 里没有自己的插件行（只有 MCP 客户端），自动更新不可用")
else:
    for row_id, row_name in own_rows:
        row_name = row_name.strip()
        if row_name.startswith(".") or "/" in row_name or "\\" in row_name:
            bad(f"{row_id}: name 写成了路径 {row_name!r}，应当写包名")
        elif row_name != pkg.get("name"):
            warn(f"{row_id}: name={row_name!r} 与本包名 {pkg.get('name')!r} 不同，确认是独立包")
        else:
            ok(f"{row_id}: name={row_name}（包名，不是相对路径）")
        if "." not in (pkg.get("exports") or {}):
            bad(f"{row_id}: 用包名当插件名，但 exports 里没有 '.'，DSH 解析不到入口")
        else:
            entry = PKG / str(pkg["exports"]["."]).lstrip("./")
            if not entry.is_file():
                bad(f"{row_id}: exports['.'] 指向的 {entry.relative_to(PKG)} 不存在")
            else:
                ok(f"{row_id}: exports['.'] -> {entry.relative_to(PKG)}")

# ── 客户端半身（dsh.client）────────────────────────────────────────────────
print("\n[4c] 客户端半身")
client = (pkg.get("dsh") or {}).get("client")
if client is None:
    warn("没有 dsh.client：设置页那一半不会出现（Host 工具仍然可用）")
else:
    exports = pkg.get("exports") or {}
    if "./client" not in exports:
        bad("声明了 dsh.client，但 exports 里没有 './client'——DSH 会报 client-modules 错")
    else:
        entry = PKG / str(exports["./client"]).lstrip("./")
        if not entry.is_file():
            bad(f"exports['./client'] 指向的 {entry.relative_to(PKG)} 不存在")
        else:
            ok(f"exports['./client'] -> {entry.relative_to(PKG)}（{entry.stat().st_size} 字节）")
            text = entry.read_text(encoding="utf-8")
            if "__ModuleLoader__.load" not in text:
                bad("客户端入口没有走 window.__ModuleLoader__.load，DSH 载入不到")
            else:
                ok("走 __ModuleLoader__.load 注册")
            # 注册 id 必须等于包名。client-modules 的 arrive() 拿包名去查
            # `this.factories.has(id)`，对不上就抛 `loaded without registering
            # "<包名>"`；那条错误只出现在浏览器控制台，宿主侧一切正常，设置页
            # 里就是干脆没有这一栏——极难查，所以在这里卡住。
            m = re.search(
                r"__ModuleLoader__\s*\.\s*load\s*\(\s*\{[^}]*?\bid\s*:\s*['\"]([^'\"]+)['\"]",
                text,
                re.S,
            )
            if m is None:
                bad("没能从 __ModuleLoader__.load({...}) 里读出 id")
            elif m.group(1) != pkg["name"]:
                bad(
                    f"__ModuleLoader__.load 的 id 是 {m.group(1)!r}，"
                    f"但包名是 {pkg['name']!r}——两者必须完全一致，"
                    "否则客户端模块永远注册不上，设置页不会出现"
                )
            else:
                ok(f"注册 id 与包名一致（{pkg['name']}）")
            for field in ("exports.apply", "exports.inject"):
                if field.split(".")[1] not in text:
                    warn(f"客户端入口没出现 {field}，确认是否由别处赋值")
    if client.get("platform") != "web":
        bad(f"dsh.client.platform 必须是 'web'，现在是 {client.get('platform')!r}")
    else:
        ok("platform=web")
    if "client.js" not in (pkg.get("files") or []):
        bad("client.js 不在 files 白名单里，装出来会缺文件")
    else:
        ok("client.js 在 files 白名单里")

# ── files 白名单里提到的文件是否都在 ────────────────────────────────────────
print("\n[5] package.json 的 files 白名单")
for pattern in pkg.get("files") or []:
    if "*" in pattern:
        hits = list(PKG.glob(pattern))
        if not hits:
            warn(f"{pattern} 没匹配到任何文件")
        else:
            ok(f"{pattern} -> {len(hits)} 个")
    elif (PKG / pattern).exists():
        ok(pattern)
    else:
        warn(f"{pattern} 不存在")

# ── 汇总 ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if errs:
    print(f"FAILED: {len(errs)} 个硬错误")
    for e in errs:
        print("  -", e)
    raise SystemExit(1)
print(f"PASS  bundle 结构自检通过（{len(warns)} 条提醒）")
for w in warns:
    print("  -", w)
