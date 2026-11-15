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
| `ui_locate_image` | Analyse an image the caller already has: locate text or detect elements, coordinates in image pixels |
| `ui_locate_marks` | Screenshot with numbered marks (and an optional named grid) so the model picks a number instead of guessing pixels |
| `ui_locate_act` | Locate, perform an action and attempt verification |
| `ui_locate_steps` | Run ordered steps and identify failures |
| `ui_locate_windows` | List top-level windows (title, handle, client rect, foreground) |
| `ui_locate_diff` | Where the screen changed since the last call, as rectangles (no image, no vision tokens) |
| `ui_locate_watch` | Record a few seconds and return one image of the keyframes, cropped to what changed |
| `ui_locate_frames` / `ui_locate_frame` | List or retrieve recent action frames |
| `list_monitors` | Display sizes, origins and indices |
| `health` | Check service readiness |
| `ui_locate_setup` | Prepare or rebuild Python dependencies and models |

Everything in the table is an MCP tool except `ui_locate_setup`, which the Host provides. The host may prefix tool names with a namespace.

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

Coordinates are absolute screen coordinates and may be negative on secondary displays. `monitor` accepts `primary`, an index string, `all`, `window:<title>` or `hwnd:<handle>`. Locate again after the UI moves.

### Analysing an image you already have

When the caller already holds the pixels — a screenshot from somewhere else, an image the user attached, a frame another tool produced — `ui_locate_image` answers without touching this desktop. The same contract is served over HTTP as `POST /locate-image`.

Request: `image_b64`, `width`, `height`, `text`, `mode` (`"locate"` or `"detect"`), plus the optional `limit`, `conf` and `tiled`. Any other key is rejected (`unsupported_field`) rather than ignored: a caller who writes `path` or `window` believes it works, and silent tolerance would teach the wrong lesson.

The response shape is fixed at contract version 1:

~~~json
{"ok": true, "contractVersion": 1, "coordinateSpace": "image-pixels",
 "image": {"width": 1280, "height": 720},
 "matches": [{"text": "Save", "confidence": 0.91,
              "box": {"x": 20, "y": 30, "width": 84, "height": 26},
              "center": {"x": 62, "y": 43}}]}
~~~

- `width` / `height` must equal the decoded image size. A mismatch is `dimension_mismatch`, not a silently rescaled answer: every coordinate the caller derives afterwards depends on that number.
- Coordinates are pixels from the top-left of the whole image — no monitor origin added, no normalisation — and every box is clipped inside the image.
- `mode: "locate"` requires non-empty `text`; `mode: "detect"` may omit it or send an empty string.
- Locate uses the same match tier as actions, not the looser one used for "just show me": exact, or the query at the start/end of the recognised text. Text where the query only appears mid-string is refused, because whoever receives a match tends to click it — a result that merely "happens to contain" the words is how the wrong window gets clicked. When the cropped OCR of detected boxes finds nothing, the whole image is re-read once before answering; a successful hit costs one pass.
- `matches` is best-first (exact before prefix/suffix, then closeness of text length, then confidence), so `matches[0]` is the intended target. Confidence is therefore not monotonically decreasing.
- No match is a success with `matches: []` and nothing is executed.
- Limits: 32 MiB request body, 24 MiB image, 40 megapixels, 16384 px per side, `limit` up to 500.
- Codes: `bad_json`, `bad_request`, `unsupported_field`, `bad_base64`, `bad_image`, `unsupported_format` (PNG and JPEG only), `too_large` (413), `dimension_mismatch`, `missing_text`, `bad_mode`, `busy` (503, engine timeout), `engine_error` (500). Failures are never cached and never return a partial match list.

The route accepts no `path`, `monitor`, `window`, `capture` or `focus`, reads no file and grabs no screen; it keeps working with the desktop side switched off.

### Comparing two images

Whether a step actually changed anything is a question about pixels, not about file bytes: the same picture re-encoded with another compressor, or carrying one extra `tEXt` chunk, has different bytes and identical pixels. `POST /compare-images` decodes both images and compares them pixel by pixel — no detector, no OCR, no screen, no model, so it still answers when the engine failed to load.

Request: `{"before": {"image_b64", "width", "height"}, "after": {...}}` — a strict whitelist, with the same size/format/volume limits as `/locate-image`. Each side's declared size must match the size in the image header (`dimension_mismatch` otherwise), and the header is checked **before** decoding, so declaring `1×1` cannot smuggle a huge image past the pixel budget.

~~~json
{"ok": true, "contractVersion": 1, "comparable": true, "changed": false,
 "changedPixels": 0, "totalPixels": 921600, "changedRatio": 0.0,
 "maxChannelDelta": 0, "changedBox": null,
 "image": {"width": 1280, "height": 720},
 "before": {"width": 1280, "height": 720},
 "after":  {"width": 1280, "height": 720}}
~~~

- `changedPixels` counts pixels where any channel differs, with **no tolerance**: one level of difference is a difference. Deciding how much change is meaningful belongs to the caller, using `changedRatio`, `maxChannelDelta` and `changedBox`.
- Different sizes are not comparable: HTTP 200 with `comparable: false` and `changed: null` (plus `reason: "dimension_mismatch"`). Not `false` — `false` would claim "I compared them and nothing moved". A resized pair is neither, and must never be recorded as a successful verification.
- `comparable: false` is still a well-formed request (both images are valid, only their sizes differ). The 400s are for malformed input: `unsupported_field`, `bad_base64`, `unsupported_format`, `dimension_mismatch` (declared ≠ decoded), `too_large`.
- `/health` advertises `capabilities.compare`, so a client can refuse to act *before* discovering halfway through that the service cannot verify the result. In managed mode this route stays available: it touches no desktop and injects no input.

### Working with one window

Background delivery needs a window handle: pass `hwnd=` (from `ui_locate_windows`) or `window=`. A bare screen point is refused with `reason: "hwnd_required"` — the service will not guess which window you meant.

Channel order inside `background` is fixed: an accessible element with `InvokePattern` goes through UI Automation (`channel: "uia-invoke"`); only a *definitive* miss (`no_pattern`, `no_element`, no UIA at all) falls through to target-window messages (`channel: "message"`). A UIA call that times out or errors is reported as `code: "BACKGROUND_UNKNOWN"` with `dispatched: null` — the action may have happened, so nothing is re-sent. Message delivery is limited to a whitelist (`#32770`, WinForms roots, and classic controls such as `Button`, `Edit`, `ComboBox`, `SysListView32`, `msctls_*`); Chromium, WinUI3, UWP, games and owner-drawn surfaces are refused with `BACKGROUND_UNAVAILABLE` rather than falling back to the foreground path. When a modal popup is open, messages go to the popup (`GetLastActivePopup`) and coordinates are converted against it, so clicks no longer land on the disabled owner.

Before the mouse sequence it may post one `WM_ACTIVATE` so the control treats itself as live — `activate="auto"` (default: only when the target is not already foreground), `never`, or `always`; this never calls `SetForegroundWindow`, but a rare application may seize the foreground on its own after receiving it, in which case pass `activate="never"`.

`ui_locate_pause(reason)` / `ui_locate_resume()` (or `POST /pause` / `/resume`) stop every input path — foreground and background — and answer with `code: "PAUSED"` until resumed; `/health` reports `paused`. The process-wide hotkey is not implemented yet.

Pass `window=` (title or part of it, or a handle) to `ui_locate_marks`, `ui_locate_act` or `ui_locate_diff`:

- Capture tries Windows Graphics Capture first: it reads the window from the compositor, so a game covered by the chat window is still captured, GPU-rendered content included, cropped to the client area. One session per window stays open while frames are being requested (closed after 15 s idle), so repeated grabs during watch/wait are nearly free. If WGC fails or returns black it falls back to `PrintWindow`, then to cropping a monitor capture. `UI_LOCATE_WINDOW_CAPTURE` / `"window_capture"`: `auto` (default), `wgc`, `printwindow`, `screen`. WGC needs the `windows-capture` package; the plugin installs it with `--no-deps` (its metadata asks for `opencv-python`, which clashes with `opencv-python-headless`). On Windows 10 a yellow capture border may show around the window; Windows 11 turns it off.
- Input channel and focus are separate decisions (`delivery=`). `auto` (default) sends input to the foreground window and, for clicks, falls back to window messages only when the cursor demonstrably did not move. `foreground` always takes the foreground path and may move the real pointer. `background` posts window messages only — it never focuses and never moves the pointer — and refuses with `BACKGROUND_UNAVAILABLE` (or `BACKGROUND_UNKNOWN` when UIA`s outcome is unknowable) when the target window cannot take them, instead of quietly escalating to SendInput. Background exists for the click family (single/double/right/middle); keyboard, scroll, drag and move need `foreground`. It is a message route, not a guarantee: some frameworks ignore posted clicks, so inspect `dispatched`. Without `window=`, mouse actions focus the window under the point; `focus="off"` disables it. `performed.focus` reports whether it worked.
- Marks taken with `window=` keep it, so `ui_locate_act(mark=…)` later focuses and verifies that window.

### Waiting and verifying

With `verify="screen"` and no `settle_ms`, an action waits until the screen stops changing (32 px grid diff, up to 1.5 s; `settle.stable` / `elapsed_ms` in the result) instead of a fixed 220 ms. The check also lists the changed `regions` and counts a strong change next to the target (`by: "region"`) when the averaged difference is diluted. `action="wait"` without text waits for a still screen. Before acting, about 80 ms of idle frames measure how much the scene moves by itself (water, particles, video); on such scenes (`verify.noise.animated`) the threshold rises with the noise and cells that were already moving are ignored, so clicking empty animated background no longer reports `verified: true`.

Results keep two questions apart: `dispatched` says whether the input reached the target (a posted message was accepted, or SendInput delivered events), `verified` says whether the screen then confirmed the expected change. `verified: false` is never retried automatically — synthetic input has side effects, and replaying a click because the pixels did not move is how the same button gets pressed twice.

### Watch, then act

Many actions have effects you cannot predict from one screenshot (does the map pan, which way does the enemy walk, is the skill on cooldown). `ui_locate_watch` records a few seconds (`duration_ms`, `fps`), picks keyframes with the grid diff, and returns **one** image: the first frame as an overview, then for each later keyframe only crops of what changed, labelled with time and screen coordinates. A still screen returns a single frame; cells that change in nearly every frame (water, particles, clocks) are treated as ambient and ignored (`ambient` in the result). `ui_locate_act(..., watch_ms=2000)` starts recording before the input is sent and stops once the action and its settle are done (at least `watch_ms`), so the motion itself is captured and "look, act, look" is one call. In `regions` mode the sheet ends with a full last frame with each keyframe's changed regions boxed, so direction and distance stay readable.

`ui_locate_marks` is incremental by default: for the same monitor or window it diffs against the previous call, reuses everything when nothing changed, and re-runs detection and OCR only on the changed regions (plus any element straddling them) when less than 30% changed. `incremental.mode` is `reuse`, `partial` or `full`; mark numbers are always reassigned. Before a partial run it estimates the cost (`incremental.estimated_cost`: `area_frac`, `text_frac`, `partial_ms` vs `full_ms`, calibrated by the last partial's measured time). OCR cost follows text, not area, so on text-dense pages where the change sits on text it goes straight to `full` (`reason: partial_not_cheaper`). `incremental=false` returns `{mode: full, forced: true}`.

## Configuration and data

The Host row ID is `ui-locate-update`; the MCP row is `mcp-ui-locate`.

| Host setting | Default | Meaning |
| --- | --- | --- |
| `pythonSync` | `true` | Synchronize the Python package with the bundle's declared version |
| `runtimeDir` | Empty | Python environment, state and logs |
| `modelDir` | Empty | Model directory; existing models can be reused |
| `backend` | Empty | Reuse the saved selection; DirectML on first preparation |

The default home follows `UI_LOCATE_HOME` → `DSH_HOME/ui-locate` → `LOCALAPPDATA/ui-locate`. Empty directory settings follow portable DSH data; explicit absolute paths require maintenance.

The Python service also reads `UI_LOCATE_MODEL`, `UI_LOCATE_MODEL_DIR`, `UI_LOCATE_PROVIDER` and `UI_LOCATE_PORT`; see [config.py](src/ui_locate/config.py). MCP connects to local port 7788 by default. Images for the model are JPEG at quality 90 with 4:4:4 chroma (no subsampling) so small colored text stays legible; vision tokens depend on pixel size, not bytes. `UI_LOCATE_JPEG_QUALITY` changes the quality.

### Screen capture

`UI_LOCATE_CAPTURE` (or `"capture"` in the state directory's `runtime.json`) picks how screenshots are taken:

| Value | Method | Use it when |
| --- | --- | --- |
| `auto` (default) | DXGI → WGC → GDI, skipping failures and all-black frames, remembering what worked per monitor | Almost always |
| `dxgi` | Desktop Duplication | Fastest; captures exclusive-fullscreen D3D games |
| `wgc` | Windows Graphics Capture (needs `dxcam[winrt]`) | Hybrid-GPU laptops where DXGI reports `UNSUPPORTED` |
| `gdi` | BitBlt | Maximum compatibility; often black for fullscreen games |

`health` reports the backend each monitor last used under `screen.capture`. A remembered fallback is re-checked against DXGI every 30 s, so one bad frame does not demote a monitor for good. HTTP requests (`/marks`, `/act`, `/steps`, `/diff`, `/detect`) may also carry `"capture": "gdi"` etc. for that request only.

If a target window runs as administrator and the server does not, Windows silently drops synthetic input (UIPI). `health.integrity` shows the server's level, `/windows` shows each window's, and actions on such a window return `uipi.blocked: true` with a warning.

### Input for games

Fullscreen games often read the mouse and keyboard through Raw Input / DirectInput and ignore what desktop apps accept. Each can be set per call (`pointer=`, `keys_mode=`) or globally:

| Setting | Values | Use it when |
| --- | --- | --- |
| `UI_LOCATE_POINTER` / `"pointer"` | `absolute` (default), `relative` | Clicks land nowhere and the cursor does not move in a fullscreen game. `relative` pushes the cursor like a real mouse, with acceleration turned off for the move and closed-loop correction; `tracking: false` in the result means the game locked the system cursor and the position is a 1:1 estimate |
| `UI_LOCATE_KEYS` / `"keys"` | `vk` (default, fills scan codes too), `scancode` | Keys do nothing in a DirectInput game |

`action="hold"` keeps keys down for `duration_ms` (walking, charging). Keyboard actions need no target; their screen check compares the whole frame.

### Managed mode

The managed switch (`MANAGED_ENV` in `src/ui_locate/managed.py`) tells the Python service that a Host owns this desktop. Every autonomous desktop capability — capture, monitor and window listing, marks, watch, diff, steps, frames, focus, input — is then refused with HTTP `403` and `{"ok": false, "code": "CUA_MANAGED", ...}` naming the route and how to reach the Host instead. The refusal runs inside the HTTP process before routing, so posting straight to `/act` is refused exactly like the MCP tool; MCP repeats the check so a stale client learns the reason without a round trip, and `/health` stops enumerating screens and probing process integrity.

Three things stay available: `GET /health`, `POST /locate-image` and `POST /compare-images`. `health` reports `contractVersion`, `managed` and `capabilities` (`{"image": true, "compare": true, "desktop": false}`), so a Host can confirm it reached an instance that enforces the boundary — and can verify results — before sending anything. A Host should treat `capabilities.desktop === false` as part of the handshake, not just `managed: true`: a `true` or a missing `desktop` key means the service that answered is not the one enforcing the boundary (for instance an older standalone process on the same port), and the safe answer is to refuse the instance rather than fall back. A health response without `contractVersion` is likewise an older process: restart it rather than use it.

The switch is read on every request, so the process that serves the request decides — not whoever spawned it. Unset it and the standalone service keeps all of its capabilities.

A Host that keeps both a managed and a standalone instance gives the managed one its own port and asks `/health` before sending anything: `7788` may still be an older standalone process that enforcement never reached.

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
- `delivery="background"` needs `hwnd=`/`window=` and covers the click family only. It is a message/UIA route, not universal background input: some frameworks ignore posted messages, and a UIA timeout is reported as `BACKGROUND_UNKNOWN` with `dispatched: null` — do not retry it blindly.
- `ui_locate_pause` stops every input path at once; `/health` keeps answering with `paused: true` while paused.
- With the managed switch on, most tools return `CUA_MANAGED` on purpose; only `/health`, `/locate-image` and `/compare-images` remain. Desktop actions belong to a standalone instance's `ui_locate_act` with `delivery="background"`.
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
