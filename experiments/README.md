# experiments

选型阶段留下的基准与试验脚本。**不是运行时的一部分**，主流程不 import 这里的任何东西。

保留它们是因为每个都承载了一个结论或一个否定——以后想重走某条路时，
先看这里的结论，别再从零试一遍。

| 脚本 | 结论 |
|------|------|
| `bench_icon_detect.py` | CPU 8 线程 142~158 ms / CUDA **10.99 ms**（快 14 倍）；TRT 不可用（缺 `nvinfer_10.dll`）；DirectML 算子不支持，全部静默回退 CPU |
| `bench_cudnn.py` | 新进程首次 CUDA 推理固定 ~35 s，`EXHAUSTIVE`/`HEURISTIC`、`use_max_workspace` 都绕不过 → **只能靠常驻服务** |
| `bench_ocr.py` | OCR 走 CPU 最快（1567×881 约 436 ms）；`max_side_len=960` 354 ms 但掉框；默认参数已是最优平衡 |
| `quantize.py` | INT8 动态量化：80.3 MB → 20.4 MB，但生成 `ConvInteger(10)`，**CPU / CUDA EP 都不实现**，模型加载失败 → CNN 量化必须用带校准集的静态 QDQ |
| `convert_fp16.py` | FP16：80.3 MB → 40.2 MB，**CUDA 11.70 ms 反而比 FP32 的 10.99 ms 慢** → 无收益，放弃。内含 `fix_resize_inputs()` / `dedupe_value_info()` 两个修坑函数（Resize scales 被转坏、value_info 重复条目） |
| `probe_providers.py` | 列出 ORT 可用 providers |
| `probe_screen.py` | DPI / 物理屏 / 虚拟桌面 / 显示器布局。开发机实测：双屏，虚拟桌面外接矩形比两屏实际像素之和大 38.7%（那块空白是显示器之间的空隙），DPI=1x |
| `probe_dpi.py` | 验证 DPI 感知开关对 PIL Grab 尺寸无影响（实测 1x 屏上恒定） |
| `probe_grab.py` | **`ImageGrab.grab(bbox=负坐标)` 对副屏返回全黑**（max<8）；`all_screens=True` 能抓到完整虚拟桌面 |
| `probe_virtual.py` | 验证 all_screens 整图里副屏区域有内容（max=255），不是黑块 |
| `calib_edge.py` | 逐块报边缘/墨水密度，用于校准 `MIN_EDGE_RATIO=0.0008`（纯色块≈0.0000、一行小字≈0.004、满屏文本≈0.05） |
| `calib_seam.py` | 造图验证切缝对齐：元素横跨 `[640,800]` 时纯几何切缝 720 会劈开，smart 挪到 612 后穿过 0 条。**判据要用 `xb[i]` 不能用 tile 的 `x0`** |
| `compare_text_mode.py` | mosaic vs full 对「窄按钮」的召回差异：mosaic 更快但正文段落认不出；两者都受图标噪声影响（OCR 把图标读成 `8` / `$` 前缀） |

跑之前注意：这些脚本用的是 `src/ui_locate/` 里的模块，需要把 `src` 放进
`PYTHONPATH`（或者直接把包 `pip install -e .` 装进环境）。

```powershell
$env:PYTHONPATH = "$PWD\src"
python experiments\bench_icon_detect.py
```

模型路径与阈值都从 `ui_locate.config` 取，可用 `UI_LOCATE_MODEL` 等环境变量覆盖，
脚本里没有写死的本机路径。
