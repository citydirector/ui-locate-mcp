"""生成 DSH 插件市场索引（format 1），让 ui-locate 能被「在线导入」。

**它解决什么**

DSH 的「插件 → 发现」页读的是一份 JSON 索引文件，不是网页、也不是目录列表。
把这份 JSON 放到任意 https 静态地址，用户在界面里添加该地址，就能像装官方
插件一样一键安装本插件 —— 这就是「在线导入」。

**索引长什么样**（format 1，字段全部经 DSH 的 parseMarketIndex 校验）

    {
      "format": 1,
      "title": {...},                    # 市场名，可多语言
      "metadata": {"categories": {}, "labels": {}, "sortKeys": {}},
      "plugins": [
        {
          "name": "<npm 包名>",
          "title": {"en": "...", "zh": "..."},
          "description": {...},
          "icon": "./icon.svg",          # 相对索引 URL 解析
          "versions": [
            {
              "version": "0.2.0",
              "publishTimestamp": 1760000000,   # Unix 秒
              "source": {"type": "git", "url": "...", "commit": "<40 位小写 hex>",
                         "path": "packages/ui-locate"},
              "engines": {"dsh": ">=0.2.1-0"}   # 可省略；留空=任何版本
            }
          ],
        }
      ]
    }

**两种发布方式**

  npm：`source` 写 `{"type": "npm"}`，安装时执行 `pnpm add <name>@<version>`。
       需要先把包发到 registry（含 npmmirror 镜像）。
  git：`source` 写 `{"type": "git", "url": ..., "commit": ...}`，安装时执行
       `pnpm add git+https://...#<commit>`。**必须固定到提交**，不能用分支名
       —— 索引校验会拒掉非 40 位 hex。

用法：
  python scripts/make_index.py --git-url https://github.com/<owner>/<repo> \\
      --commit <sha> --path packages/ui-locate --out market-index.json

  # 已发布到 npm 时
  python scripts/make_index.py --npm --out market-index.json

  # 只校验一份现有索引的结构
  python scripts/make_index.py --check market-index.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
COMMIT = re.compile(r"^[0-9a-f]{40}$")
# 与 DSH @deepseek-ai/dsh-plugin-market-index 的 PACKAGE_NAME 逐字一致
PACKAGE_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")
MAX_PUBLISH_TIMESTAMP = 10**11


def read_manifest() -> dict:
    return json.loads((ROOT / "package.json").read_text(encoding="utf-8"))


def read_locale(name: str) -> dict:
    p = ROOT / "locale" / f"{name}.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("meta", {})


def localized(field: str) -> dict:
    """把各语言 locale 里的同名字段汇成索引要的多语言对象。"""
    out: dict[str, str] = {}
    for lang in ("en", "zh"):
        v = read_locale(lang).get(field)
        if v:
            out[lang] = v
    return out


def build(args: argparse.Namespace) -> dict:
    manifest = read_manifest()
    name = manifest["name"]
    version = manifest["version"]

    if not PACKAGE_NAME.match(name):
        raise SystemExit(f"package.json 的 name 不是合法的 npm 包名：{name}")
    if not SEMVER.match(version):
        raise SystemExit(f"package.json 的 version 不是精确 SemVer：{version}")

    if args.npm:
        source: dict = {"type": "npm"}
        if args.registry:
            source["registry"] = args.registry
    else:
        if not args.git_url:
            raise SystemExit("用 git 发布时必须给 --git-url")
        if not args.commit or not COMMIT.match(args.commit):
            raise SystemExit("--commit 必须是 40 位小写 hex 的提交 SHA")
        source = {"type": "git", "url": args.git_url, "commit": args.commit}
        if args.path:
            source["path"] = args.path

    engines: dict = {}
    if args.dsh_range:
        engines["dsh"] = args.dsh_range

    version_entry: dict = {
        "version": version,
        "publishTimestamp": int(args.timestamp or time.time()),
        "source": source,
    }
    if engines:
        version_entry["engines"] = engines

    plugin_entry: dict = {
        "name": name,
        "title": localized("title") or {"en": name},
        "description": localized("description") or {"en": manifest.get("description", "")},
        "icon": args.icon,
        "metadata": {},
        "versions": [version_entry],
    }
    if args.author:
        plugin_entry["author"] = args.author
    if args.homepage:
        plugin_entry["homepage"] = args.homepage

    return {
        "format": 1,
        "title": args.market_title,
        "description": args.market_description,
        "metadata": {
            "categories": {
                "automation": {"title": {"en": "Automation", "zh": "自动化"}}
            },
            "labels": {
                "windows": {"title": {"en": "Windows", "zh": "Windows"}},
                "local": {"title": {"en": "Local", "zh": "本地"}},
            },
            "sortKeys": {
                "published": {"title": {"en": "Published", "zh": "发布时间"}, "order": "desc"}
            },
        },
        "plugins": [plugin_entry],
    }


# ── 校验：按 DSH parseMarketIndex 的规则检查一遍 ─────────────────────────────


def check(doc: dict, index_url: str = "https://example.com/market-index.json") -> list[str]:
    errs: list[str] = []

    def fail(path: str, reason: str) -> None:
        errs.append(f"{path}: {reason}")

    if doc.get("format") != 1:
        fail("$.format", "must be 1")
    if not isinstance(doc.get("title"), (str, dict)):
        fail("$.title", "must be a string or a localized object")
    # 官方 stringAt() 要求「非空且含非空白字符」——空字符串会被拒
    desc = doc.get("description")
    if isinstance(desc, str) and not desc.strip():
        fail("$.description", "must be a string with a non-whitespace character")
    plugins = doc.get("plugins")
    if not isinstance(plugins, list):
        fail("$.plugins", "must be an array")
        return errs

    names: set[str] = set()
    for i, p in enumerate(plugins):
        at = f"$.plugins[{i}]"
        if not isinstance(p, dict):
            fail(at, "must be an object")
            continue
        nm = p.get("name")
        if not isinstance(nm, str) or not PACKAGE_NAME.match(nm):
            fail(f"{at}.name", "must be a valid npm package name")
        elif nm in names:
            fail(f"{at}.name", f"duplicates {nm}")
        else:
            names.add(nm)

        vers = p.get("versions")
        if not isinstance(vers, list) or not vers:
            fail(f"{at}.versions", "must be a non-empty array")
            continue
        seen: set[str] = set()
        for j, v in enumerate(vers):
            vat = f"{at}.versions[{j}]"
            ver = v.get("version")
            if not isinstance(ver, str) or not SEMVER.match(ver):
                fail(f"{vat}.version", "must be an exact SemVer version")
            elif ver in seen:
                fail(f"{vat}.version", f"duplicates {ver}")
            else:
                seen.add(ver)

            ts = v.get("publishTimestamp")
            if not isinstance(ts, int) or ts < 0 or ts >= MAX_PUBLISH_TIMESTAMP:
                fail(f"{vat}.publishTimestamp", "must be integer Unix seconds in [0, 10^11)")

            src = v.get("source") or {}
            t = src.get("type")
            if t == "npm":
                reg = src.get("registry")
                if reg is not None and not str(reg).startswith(("http://", "https://")):
                    fail(f"{vat}.source.registry", "must be an http(s) URL")
            elif t == "git":
                url = src.get("url")
                if not isinstance(url, str) or not url.startswith("https://"):
                    fail(f"{vat}.source.url", "must be an https URL")
                commit = src.get("commit")
                if not isinstance(commit, str) or not COMMIT.match(commit):
                    fail(f"{vat}.source.commit", "must be a lowercase 40-hex-digit commit SHA")
                if "path" in src and not isinstance(src["path"], str):
                    fail(f"{vat}.source.path", "must be a string")
            else:
                fail(f"{vat}.source.type", 'must be "npm" or "git"')

            eng = v.get("engines") or {}
            if "dsh" in eng and not isinstance(eng["dsh"], str):
                fail(f"{vat}.engines.dsh", "must be a SemVer range string")

        for field in ("title", "description"):
            val = p.get(field)
            if isinstance(val, dict) and "en" not in val:
                fail(f"{at}.{field}", "must contain en")

        ic = p.get("icon")
        if ic is not None:
            if not isinstance(ic, str):
                fail(f"{at}.icon", "must be a string")
            elif not ic.startswith("data:"):
                if ic.startswith("http://"):
                    fail(f"{at}.icon", "must resolve to https or the index origin")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description="生成/校验 DSH 插件市场索引")
    ap.add_argument("--npm", action="store_true", help="source 用 npm registry")
    ap.add_argument("--registry", default="", help="npm registry URL（可选）")
    ap.add_argument("--git-url", default="", help="git 仓库 https URL")
    ap.add_argument("--commit", default="", help="40 位小写 hex 提交 SHA")
    ap.add_argument("--path", default="", help="仓库内的包子目录（可选）")
    ap.add_argument(
        "--dsh-range",
        default="",
        help='engines.dsh，如 ">=0.2.1-0"。留空表示任何 DSH 版本都能装（推荐）',
    )
    ap.add_argument("--icon", default="./icon.svg", help="图标引用，相对索引 URL")
    ap.add_argument("--author", default="")
    ap.add_argument("--homepage", default="")
    ap.add_argument("--timestamp", type=int, default=0, help="发布 Unix 秒，缺省取当前时间")
    ap.add_argument("--market-title", default={"en": "Local Plugins", "zh": "本地插件"})
    ap.add_argument(
        "--market-description",
        default={"en": "Local plugins that run on this machine.", "zh": "在本机运行的本地插件。"},
        help="市场描述；空字符串会被官方校验拒掉（stringAt 要求非空白）",
    )
    ap.add_argument("--out", type=Path, default=ROOT / "market-index.json")
    ap.add_argument("--check", type=Path, default=None, help="只校验已有索引")
    args = ap.parse_args()

    if args.check:
        doc = json.loads(args.check.read_text(encoding="utf-8"))
        errs = check(doc)
        if errs:
            print("校验失败：")
            for e in errs:
                print("  -", e)
            return 1
        print("校验通过：format 1，结构符合 DSH parseMarketIndex 的规则")
        return 0

    doc = build(args)
    errs = check(doc)
    if errs:
        print("生成的索引自身校验失败：")
        for e in errs:
            print("  -", e)
        return 1

    args.out.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已写出 {args.out}")
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
