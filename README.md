# ui-locate-mcp

**English** · [简体中文](README.zh-CN.md)

[![npm](https://img.shields.io/npm/v/ui-locate-mcp)](https://www.npmjs.com/package/ui-locate-mcp) [![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![DSH MCP plugin](https://img.shields.io/badge/DSH%20MCP%20plugin-Windows-blue)](https://github.com/topics/dsh-plugin)

Find on-screen UI text locally and return pixel coordinates for desktop interaction. Combined tools can locate, click, type, scroll and attempt to verify the result.

Detection, OCR and actions run locally. Initial setup downloads Python dependencies and a detection model of about 80 MB. A persistent service runs inference; a lightweight MCP process forwards requests.

## Quick start

1. Enter `ui-locate-mcp` in DSH **Plugins → Add plugin**, then enable it.
2. Open **ui-locate runtime** in Settings, choose paths and a backend, and prepare the environment. The Host tool `ui_locate_setup` provides the same preparation path.
3. After preparation, connect or re-enable `mcp-ui-locate`. Restart if DSH requests it.
4. Call `health`, then try `locate_text(text="Save")`.

The bundle contains an MCP client, a Host runtime-management plugin and a Client settings page. npm installation supplies the code; Python dependencies and the model require separate setup.

### Requirements

- The current bundle's MCP executable path is configured for **Windows**. Cross-platform Python code does not imply a cross-platform bundle configuration.
- The Python project requires **Python 3.12+**. The settings preparation uses a Python runtime located by the host to create a dedicated environment. Check the desktop installation's bundled runtime if no interpreter can be found.
- **DirectML** is the default backend; `cuda` and `cpu` are alternatives. CUDA needs the corresponding NVIDIA environment and larger dependencies. Changing backends requires rebuilding the environment.
- package.json currently declares no `engines.dsh` range. This is not a guarantee of compatibility with every DSH version: the bundle needs tool registration, an MCP client and settings services.

For a Web profile:

~~~sh
dsh plugin --profile web add ui-locate-mcp
~~~

Stop the instance before CLI changes and restart afterwards. Prefer the Plugins page on Desktop; managing its desktop profile requires that installation's CLI and the same DSH_HOME.

## Tools

| Tool | Purpose |
| --- | --- |
| `locate_text` | Find matching UI text and its center coordinates |
| `find_all` | Return multiple matching candidates |
| `detect_elements` | Enumerate detected elements |
| `ui_locate_marks` | Screenshot with numbered marks (and an optional named grid) so the model picks a number instead of guessing pixels |
| `ui_locate_act` | Locate, perform an action and attempt verification |
| `ui_locate_steps` | Run ordered steps and identify failures |
| `ui_locate_frames` / `ui_locate_frame` | List or retrieve recent action frames |
| `list_monitors` | Display sizes, origins and indices |
| `health` | Check service readiness |
| `ui_locate_setup` | Prepare or rebuild Python dependencies and models |

The first ten entries come from MCP; the last two are Host tools. The host may prefix tool names with a namespace.

~~~python
locate_text(text="Save", monitor="primary")
ui_locate_act(text="Save", action="click", monitor="primary")
~~~

For targets without stable text (game items, toolbar icons, map units), mark the screen and act by number:

~~~python
ui_locate_marks(monitor="primary")          # image with numbered boxes
ui_locate_act(mark=17)                       # click #17's detector-computed center
ui_locate_marks(box="1200,40,1500,90")       # zoom a strip of tiny buttons (up to 4x)
ui_locate_marks(grid=8)                      # nothing boxed? pick a grid cell, e.g. D3
ui_locate_marks(box="D3", grid=4)            # zoom that cell, then pick a mark or sub-cell
ui_locate_act(cell="B2")                     # click a cell's center
~~~

Besides detector boxes, marks include repeated same-size cells found by geometry alone (inventory slots, tabs, skill bars, tables; `kind: "cell"`), which UI detectors usually miss in games. Numbers belong to one marked image; pass `mark_set` to refuse a stale one.

Coordinates are absolute screen coordinates and may be negative on secondary displays. `monitor` accepts `primary`, an index string or `all`. Locate again after the UI moves.

## Configuration and data

The Host row ID is `ui-locate-update`; the MCP row is `mcp-ui-locate`.

| Host setting | Default | Meaning |
| --- | --- | --- |
| `pythonSync` | `true` | Synchronize the Python package with the bundle's declared version |
| `runtimeDir` | Empty | Python environment, state and logs |
| `modelDir` | Empty | Model directory; existing models can be reused |
| `backend` | Empty | Reuse the saved selection; DirectML on first preparation |

The default home follows `UI_LOCATE_HOME` → `DSH_HOME/ui-locate` → `LOCALAPPDATA/ui-locate`. Empty directory settings follow portable DSH data; explicit absolute paths require maintenance.

The Python service also reads `UI_LOCATE_MODEL`, `UI_LOCATE_MODEL_DIR`, `UI_LOCATE_PROVIDER` and `UI_LOCATE_PORT`; see [config.py](src/ui_locate/config.py). MCP connects to local port 7788 by default.

### Screen capture

`UI_LOCATE_CAPTURE` (or `"capture"` in the state directory's `runtime.json`) picks how screenshots are taken:

| Value | Method | Use it when |
| --- | --- | --- |
| `auto` (default) | DXGI → WGC → GDI, skipping failures and all-black frames, remembering what worked per monitor | Almost always |
| `dxgi` | Desktop Duplication | Fastest; captures exclusive-fullscreen D3D games |
| `wgc` | Windows Graphics Capture (needs `dxcam[winrt]`) | Hybrid-GPU laptops where DXGI reports `UNSUPPORTED` |
| `gdi` | BitBlt | Maximum compatibility; often black for fullscreen games |

`health` reports the backend each monitor last used under `screen.capture`.

## Updating

**The plugin itself is updated by the DSH plugin page; this package no longer ships
an updater of its own.** The dependency is a registry spec (`ui-locate-mcp`), so that
row grows a native update button backed by the host's own plugin manager.

What this package still owns is the half the host does not: after npm swaps the
package, the copy inside the venv is still the old one. Every startup compares the
two versions and reinstalls with `pip install --no-deps` when they differ (turn it
off with `pythonSync`). Running it at startup is also self-healing — a failure here
does not undo the JS side, and the next start tries again.

In 0.5.x and earlier this repository carried a whole updater of its own (read a version
list, then call `installBundle`). It was removed because it was a second update path
running alongside the plugin page: the two disagreed about what was latest, and its
installs rewrote the dependency back to `github:`, which made the plugin page's
button disappear. See the [update audit](docs/update-audit.md).

## Troubleshooting and limits

- If MCP will not start, check environment preparation and the virtual-environment Python before reconnecting.
- `ok` means the action pipeline completed; inspect verification separately. `verified: false` or `null` is not confirmation that the intended action succeeded.
- Desktop permissions, focus, DPI and application frameworks affect input and capture. Locating a target does not guarantee it accepts synthetic input.
- ONNX Runtime backend wheels share the same Python package name; rebuild rather than installing competing backends together.
- OCR can miss or misread text. Inspect the screen or target text after important actions.

## Development

~~~sh
python tests/check_bundle.py
node --test tests/
python -m unittest discover -s tests -p "test_*.py"
~~~

Python tests need their dependencies; real-model and desktop smoke tests need a prepared environment. Historical measurements and algorithm notes are in [technical notes](docs/technical-notes.md), not a performance guarantee for other machines.

The current [release.yml](.github/workflows/release.yml) publishes on version tags or manual dispatch with a tag. Keep package.json and pyproject.toml versions aligned when releasing.

## Related projects

More DeepSeek Harness (DSH) projects from [@citydirector](https://github.com/citydirector):

| Project | Kind | What it does |
|---|---|---|
| [DSH-build](https://github.com/citydirector/DSH-build) | Build | Portable and desktop Windows builds of DSH, patched, smoke-tested and released daily |
| [dsh-remote-access-proxy](https://github.com/citydirector/dsh-remote-access-proxy) | Plugin | Use the DSH Web GUI from your phone, tablet, or another computer |
| [deepinfra-fusion-proxy](https://github.com/citydirector/deepinfra-fusion-proxy) | Plugin | DeepInfra Standard / Flex switching with automatic fallback, per session |
| [dsh-loop-rescue](https://github.com/citydirector/dsh-loop-rescue) | Plugin | Cut model repetition loops mid-stream, prune them from the log, and resume |
| [dsh-pwsh-plugin](https://github.com/citydirector/dsh-pwsh-plugin) | Plugin | Persistent pwsh session tool (`pwsh_run`), plus a WSL write boundary for `bash` |
| [dsh-session-manager](https://github.com/citydirector/dsh-session-manager) | Offline tool | Upgrade, archive, trash, loop cleanup and repair for DSH sessions |

## License

[MIT](LICENSE)
