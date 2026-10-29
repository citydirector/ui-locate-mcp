# 更新机制核对（2026-10-29）

当前 main 与 npm 0.5.1 仍保留插件自身的更新路径，尚未完全交由 DSH 原生更新管理。

- [plugin/index.mjs](../plugin/index.mjs) 的 apply 创建 tracker，默认调用 tracker.schedule；注册 ui_locate_update，apply 动作调用 manager.installBundle。
- [plugin/core.mjs](../plugin/core.mjs) 的 fetchPublishedVersions 从 npm registry 读取版本。部分源码注释和工具描述仍写 GitHub 标签，但实际请求来源已经是 npm。
- [cordis.patch.yml](../cordis.patch.yml) 仍默认启用 updateCheck 和 pythonSync。
- pythonSync 负责同步 venv 里的 Python 包，作用不同于 npm bundle 更新；后续清理更新器时不能误删环境同步能力。

本轮仅更新文档，未更改这些运行路径。
