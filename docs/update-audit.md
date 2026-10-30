# 更新机制核对（2026-10-29，0.6.0）

0.6.0 起，插件自身的新版本**只由 DSH 插件页管理**，本包不再有第二条更新链路。

## 现在的分工

| 谁升 | 谁来管 | 代码位置 |
|---|---|---|
| 插件本身（JS / YAML） | DSH 插件页的原生更新按钮 | 宿主 `pluginManager` |
| venv 里的 Python 包 | Host 半身，启动时对齐 | `plugin/core.mjs` 的 `syncPythonToBundle` |

## 为什么删掉自研更新器

0.5.x 有一整套：`createUpdateTracker` 定时查、`fetchPublishedVersions` 读 registry、
`ui_locate_update` 调 `installBundle` 安装。它和插件页的按钮是**两条并行链路**：

1. 两边各自判断「什么是最新」，可以给出不同答案。
2. `installBundle` 收到 `git+https://...#vX.Y.Z` 会把 profile 的依赖写回
   `github:owner/repo`，而 `updateRefusal()` 要求 `kind === "registry"` ——
   插件页的更新按钮随即消失。表现是「升一次好一次、再升一次坏掉」。

0.6.0 删掉的是：`updateCheck` 配置项、`createUpdateTracker`、`fetchPublishedVersions`、
`updateVerdict` / `latestRelease` / `parseReleaseTag` / `compareVersions`、
`ui_locate_update` 工具、`pluginManager` 按需注入与两个 update timer effect。

## 刻意保留的能力

**`pythonSync` 与环境同步一个字都没动。** npm 换包之后 venv 里的 Python 包仍是旧的，
那是 pip 的地盘，宿主不会去动它。`syncPythonToBundle` 在每次启动时比对
`venv` 里的版本与包内 `pyproject.toml` 声明的版本，不一致就
`pip install --no-deps` 重装一次（只换代码不动依赖，否则三个 ORT wheel 会互相覆盖）。

## 发布链路（未变）

`git tag -a vX.Y.Z` + `git push origin vX.Y.Z` → GitHub Actions 用 OIDC trusted
publishing 发到 npm，不需要任何 token。`scripts/release-check.mjs` 会先核对
「版本号 / 索引 / 标签 / 工作区干净」四件事。
