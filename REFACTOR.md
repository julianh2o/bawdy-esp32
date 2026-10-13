# Refactor plan

Back to basics: two new packages with clear jobs, `wled` and `lightd`, replacing the current mix of `wled_app`,
`wled_engine` and the universe/patch model. They're built **alongside the old code**; Docker and entry points switch
over when they're ready, and only then is the retired code deleted.

```
                ┌────────────────────────────────────────────────┐
 web UI ──REST─▶│ lightd                                         │
                │  controllers.json · discovery (owns both)      │
                │  runs: targets ("wled_01/2") + Lua pattern     │
                │  ~30 fps: pixel(n, t, p) → RGB per channel     │
                └───────────────┬────────────────────────────────┘
                                │ WledController.send_channels(...)
                ┌───────────────▼────────────────────────────────┐
                │ wled                                           │
                │  mDNS discovery · board info/channels (live)   │
                │  DDP output · identify · JSON API state        │
                └───────────────┬────────────────────────────────┘
                                │ DDP (UDP 4048) / HTTP JSON API
                             WLED boards
```

## Status

| Phase | State |
|---|---|
| 1. `wled` | **Done**: `src/wled/` (discovery, HTTP, DDP, `WledController`), tests in `tests/test_wled.py` |
| 2. Lua runtime | **Done**: `src/lightd/runtime.lua` (shared with the browser) + `lua.py`; 10 built-in patterns in `patterns/` |
| 3. `lightd` | **Done**: `python -m lightd`; tests in `tests/test_lightd.py` |
| 4. Web | **Done**: pattern editor at `/` (also `/patterns`); `/config` uses lightd's config API (`/api/config`, `/api/discovered`, `/api/identify`). The web server is static files + the `/engine/*` proxy; the old web sequencer is removed |
| 5. Switch over | **Done for Docker**: the daemon image runs lightd with `config/` mounted read-write. Old entry points (`run.py`, `run-stack`) remain until phase 6 |
| 6. Delete retired code | Not started |

Defaults chosen for the open items: port 8765, user patterns in `config/patterns/`, run state in
`config/lightd-state.json`, budget 2,000,000 Lua instructions per channel per frame.

## Decisions

| Topic | Decision |
|---|---|
| Daemon package | `lightd` |
| `wled` API style | Synchronous methods; discovery runs in a background thread with callbacks |
| Pattern language | Lua 5.4 via [`lupa`](https://pypi.org/project/lupa/) |
| Pattern signature | `pixel(n, t, p)` returns `r, g, b` and optionally `w` (white). There are no x/y coordinates |
| `n` | 0-based pixel index within the target channel. Flipped for channels marked `reversed` |
| Target length | Available to the pattern as the global `COUNT` |
| `t` | Unix time in seconds, so every channel and run is in phase and patterns can tell the time |
| Color output | Floats 0..1, clamped; converted to bytes by `lightd` |
| Stateful effects | None. Patterns stay pure; deterministic `noise()`/`hash()` helpers stand in for randomness |
| Params | Declared with a typed schema (type, default, min/max). min/max only set the UI slider's range; typed values can go beyond it |
| Live inputs (sound, MIDI…) | Not implemented. They'll arrive later as more keys in `p` |
| Param changes | Applied live on the next frame. Since `t` is large, changing a rate param (speed, period) jumps the phase |
| Pattern library | Built-ins in the repo's `patterns/`; user patterns saved through the API into a writable dir |
| Ad-hoc Lua | Allowed: a run can carry its own source |
| Pattern errors / overruns | Skip that frame and keep trying; report the error in status |
| Targets | `controller/number` only, e.g. `wled_01/2` (1-based, as on /config) |
| Multiple targets | One run can list several targets; they share `t` and params, and `n` restarts on each |
| Sub-channel ranges | Not yet. The smallest target is a whole channel |
| Starting a run on a busy channel | The new run replaces the old one instantly (cut) |
| Idle channels | Sent black |
| Brightness / gamma | Per-channel brightness (0..1) in `lightd`, applied after rendering and kept across runs; gamma left to WLED |
| RGBW outputs | A board with any white-capable channel gets RGBW DDP frames (data type 0x1A): RGBW strips get the pattern's `w`; RGB-only channels get `w` added to r, g, b |
| Channel layout | Read live from the board: one channel per WLED segment (`/json/state`), or per LED output (`/json/cfg`) if there are no segments. A board that can't be read gets no output |
| Govee lights | Segments named "Govee RGB" and "Govee White" (same suffix, if any) are one linked channel: r, g, b go to the RGB LEDs, `w` (averaged to fit) to every byte of the white LEDs |
| Offline boards / IP changes | `lightd` runs mDNS discovery and follows boards by MAC/host |
| Restart behaviour | Active runs and channel brightness are saved and restored on start |
| Scenes | Named sets of `{targets, pattern, params, brightness?}` in `config/scenes.json`; activating one starts every entry and leaves other channels alone |
| Frame rate | Configurable, default 30 |
| Security | Trusted LAN, no auth; the Lua sandbox only guards against mistakes |
| Discovery + `controllers.json` writes | Owned by `lightd`. The web server serves static files and proxies to `lightd` |
| Web UI, first version | `/config` page and a pattern editor (code editor, in-browser preview, run on target) |
| Browser Lua | [wasmoon](https://github.com/ceifa/wasmoon), Lua 5.4 in WASM, so behavior matches `lupa` |
| Timeline sequencer (web) | Removed; a timeline may come back later on top of runs and scenes |
| Java sequencer | Kept; ported to the new API later |
| Universe / patch / fixtures | Parked: left in the repo, unused |
| Simulator | Parked |
| Tests | Unit tests only; hardware is checked by hand |

## 1. `wled`: low-level controller library

Knows about WLED boards and nothing about patterns, runs or config files.

- **Discovery**: browses mDNS `_wled._tcp.local.` with `zeroconf` and reports boards found, changed (new IP) or
  gone. `probe(ip)` reaches boards on other subnets.
- **`WledController`** is built from an IP/host or a discovery result. It loads:
  - from `/json/info`: name, MAC, version, arch, total LED count
  - LED outputs from `/json/cfg` `hw.led.ins`: start LED, length, GPIO pins, color order, RGB/RGBW
  - segments from `/json/state` `seg`; **channels** are the segments (pins/order/RGBW from the output each starts
    in), with Govee RGB + White pairs linked into one channel; without segments, the LED outputs
- **Output**: DDP over UDP 4048, at most 480 px per packet, with the push flag on the last packet.
  - `send_frame(bytes)` sends the whole board.
  - `send_channels({index: pixel_bytes})` places each channel at its start LED. Buffers are RGB, or RGBW for
    channels with `bytes_per_pixel == 4` (RGBW strips, linked Govee channels).
- **Low-level tasks**:
  - identify (blink the whole board or one channel)
  - JSON API state (on/off, brightness, leave realtime mode)
  - `refresh()`

```python
from wled import WledBrowser, WledController

browser = WledBrowser(on_change=print)    # background thread
browser.start()

board = WledController("10.10.1.111")
board.refresh()
board.channels      # [Channel(index=0, start=0, length=270, pins=[5], order="GRB", rgbw=False), ...]
board.send_channels({0: ch0_rgb, 2: ch2_rgb})
board.identify(channel=1, seconds=6)
```

```
src/wled/
  __init__.py      WledBrowser, WledController, Channel, BoardInfo
  discovery.py     mDNS browser + probe(ip)
  controller.py    WledController
  http.py          /json/info, /json/cfg, /json/state client with timeouts
  ddp.py           DDP packet encoding
  transport.py     UDP sender + in-memory fake for tests
  models.py        BoardInfo, Channel
```

## 2. `lightd`: controllers and pattern runner

- **Config**: loads, validates and saves `controllers.json` (atomic writes); reloads on request.
- **Registry**: a `WledController` per configured ID, kept up to date by discovery (MAC/host matching), with
  online/offline state.
- **Targets**: `wled_01/2` → (controller, channel). Channel order comes from the board, and a target whose board can't
  be read is skipped.
- **Runs**: a pattern plus params plus one or more targets.
  - Starting a run cuts any run on those channels.
  - Runs are saved to a state file and restored on start.
- **Loop** (default 30 fps): for each run and each target channel, render `pixel(n, t, p)` for every pixel. Then
  assemble each controller's channels, sending black for idle ones, and call `send_channels`.
- **REST API**, which also serves the web UI.

### `controllers.json`

```json
{
  "wled_01": {
    "ip": "10.10.1.111",
    "port": 4048,
    "name": "WLED",
    "mac": "68:fe:71:a4:ad:b4",
    "host": "wled-A4ADB4.local",
    "channels": [
      { "name": "Front truss" },
      { "name": "Back wall", "reversed": true }
    ]
  }
}
```

Channel lengths are not stored here; they come from the board.

### REST API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | fps, controllers online, active runs, pattern errors |
| GET | `/api/controllers` | configured controllers with live channels and online state |
| PUT | `/api/controllers` | save `controllers.json` (validated) and apply |
| GET | `/api/discovered` | boards seen by mDNS, adopted or not |
| POST | `/api/discovered/rescan`, `/api/discovered/probe` | rescan; probe an IP |
| POST | `/api/identify` | `{target}` or `{ip}` → blink |
| GET | `/api/patterns` | library: name, params schema |
| GET/PUT/DELETE | `/api/patterns/{name}` | read / save / delete a user pattern (built-ins are read-only) |
| POST | `/api/runs` | `{targets: [...], pattern: name \| source: lua, params}` → run id |
| PATCH | `/api/runs/{id}` | update params live |
| DELETE | `/api/runs/{id}` | stop a run; its channels go black |
| GET | `/api/brightness` | channels whose brightness isn't 1: `{"wled_01/2": 0.5}` |
| PUT | `/api/brightness` | `{targets: [...], brightness: 0..1}` |
| GET | `/api/scenes` | scenes with their entries and whether each is active |
| GET/PUT/DELETE | `/api/scenes/{name}` | read / save (`{entries: [...]}`) / delete a scene |
| POST | `/api/scenes/{name}/activate` | start every entry; returns the runs and any entries that failed |
| POST | `/api/blackout` | stop all runs |

### Layout

```
src/lightd/
  __init__.py
  config.py          controllers.json load/validate/save
  registry.py        id → WledController; discovery matching; online state
  targets.py         parse/resolve "wled_01/2"
  patterns.py        library: built-in + user dirs, schema extraction
  lua.py             lupa sandbox, prelude, frame rendering
  runs.py            run model, replace-on-overlap, persistence
  loop.py            fixed-rate render/send loop
  api.py             REST server
  __main__.py        python -m lightd --controllers config/controllers.json --fps 30
patterns/            built-in Lua patterns (rainbow.lua, chase.lua, ...)
```

## 3. Patterns (Lua)

```lua
-- chase.lua
params = {
  speed = { type = "number", default = 30, min = 0, max = 300 },   -- pixels per second
  width = { type = "number", default = 10, min = 1, max = 100 },
  color = { type = "color",  default = "#ff4000" },
}

function pixel(n, t, p)
  local head = (t * p.speed) % COUNT
  local d = (head - n) % COUNT
  if d < p.width then
    local k = 1 - d / p.width
    return p.color.r * k, p.color.g * k, p.color.b * k
  end
  return 0, 0, 0
end
```

- **Contract**: `pixel(n, t, p)` returns `r, g, b` floats in 0..1, plus an optional `w` (white) that lights white
  LEDs where the channel has them and is added to `r, g, b` where it doesn't. If a pattern leaves `w` out, white-capable
  channels get it extracted: `w = min(r, g, b)`, subtracted from each, so saturated colors stay on RGB and whites and
  pastels use the white chip. The global `WHITE` is true while rendering for white-capable LEDs (the `white` pattern
  uses it to tint the white chip, whose temperature is a param).
  - `n` runs `0..COUNT-1` along the channel and is flipped when the channel is `reversed`.
  - `COUNT` is a global set per target.
  - `t` is Unix time in seconds (wall clock), the same for every channel and run.
  - `p` holds the params. Defaults are filled in from the schema, and `color` params arrive as `{r, g, b}` in 0..1.
- **Param types**: `number` (default/min/max/step), `color` (hex default), `boolean`, `choice` (options). Extend as
  needed.
- **Prelude helpers**: `hsv(h, s, v)`, `mix(a, b, k)`, `clamp(x, lo, hi)`, `noise(x, y)`, `hash(n)`, plus `math`.
  These are shared by `lupa` and wasmoon so previews match the LEDs.
- **Rendering**: each channel is rendered in one call into Lua: a loop over `n` returns one byte string per frame. This
  avoids a Python↔Lua crossing per pixel.
- **Sandbox**: no `io`, `os`, `require`, `load`, `dofile` or `debug`.
  - An instruction-count hook enforces a per-frame budget.
  - On an error or overrun, that frame keeps the channel's previous output and the error is reported in
    `/api/status`. The run keeps going.

## 4. Web UI

The web server (`wled_sequencer/server.py`, slimmed down) serves static files and proxies `/api/*` to `lightd`.

- **`/config`**: today's page, pointed at `lightd`. It covers adopting boards, naming channels, the `reversed` flag
  and identify.
- **Pattern editor**:
  - CodeMirror with Lua highlighting and error line markers
  - an in-browser preview strip rendered with wasmoon and the same prelude
  - **run on target(s)**, which sends the editor's source as an ad-hoc run
  - save to the library

## Repository layout after the refactor

```
src/
  wled/               new: low-level controller library
  lightd/             new: daemon
  wled_sequencer/     web server: pattern editor (/) + /config
  wled_engine/        parked pieces only (patch, spatial, sequence_runner) until deleted or revived
  wled_simulator/     parked
patterns/             built-in Lua patterns
config/               controllers.json (+ .example); user patterns dir; lightd state file
sequencer-java/       kept; to be ported to the lightd API later
tests/
```

## Sources to migrate

### Into `wled`

| From | To | Notes |
|---|---|---|
| `wled_sequencer/controller_config.py`: `WledDiscovery`, `probe_wled`, MAC/host matching | `wled/discovery.py`, `wled/http.py` | Already mDNS + probe |
| `wled_app/protocols/ddp.py` (`DdpEmitter`) | `wled/ddp.py` | Keep chunking/push behavior |
| `wled_sequencer/identify.py`, `wled_engine/identify.py` | `wled/controller.py` | One identify; keep `led_start` for single channels |
| `wled_app/network/udp_client.py` | `wled/transport.py` | Mock becomes the test fake |
| `wled_engine/fleet/dispatcher.py` | `wled/controller.py` | Per-board send |

### Into `lightd`

| From | To | Notes |
|---|---|---|
| `wled_sequencer/controller_config.py`: `validate_controllers`, `write_json_atomic`, `ControllerConfigStore` | `lightd/config.py` | Add `reversed` per channel |
| `wled_sequencer/server.py`: config/discovery/identify/probe endpoints | `lightd/api.py` | Move from the web server to the daemon |
| `wled_engine/core.py`: loop, config lock / hot swap, blackout on stop | `lightd/loop.py` | |
| `wled_engine/clock.py` | `lightd/loop.py` | Fixed-rate tick, actual fps |
| `wled_engine/adapters/rest_daemon.py` | `lightd/api.py` | HTTP server shell, CORS, JSON handling |
| `wled_engine/daemon_config.py`, `controllers.py` | `lightd/config.py` | Drop the `patch.json` `controllers` fallback |
| `wled_app/ui/cli.py` `cmd_daemon` | `lightd/__main__.py` | SIGTERM handling, startup summary |

### Into Lua (`patterns/*.lua`, prelude)

| From | Notes |
|---|---|
| `wled_app/patterns/` rainbow, gradient, wave, solid, blink, chase, cylon, color_wipe | Stateless; rewrite as `pixel(n, t, p)` |
| `wled_app/patterns/` fire, twinkle, meteor | Re-imagine with `noise()`/`hash()` |
| `wled_engine/spatial/patterns.py` | 3-D ideas; 1-D versions only (no x/y) |
| `wled_app/domain/color.py`, `domain/palette.py` | HSV/mix helpers and palettes become the prelude |

### Parked

| What | Revisit when |
|---|---|
| `wled_engine/sequence_runner.py` | Same |
| `wled_engine/patch/`, `wled_engine/spatial/`, `universe.json`, `patch.json`, `data/presets/`, `data/venue/` | If geometry (x/y) comes back |
| `wled_simulator/` | If a no-hardware test target is needed |
| `sequencer-java/` | Porting it to the `lightd` API |

### Retire (delete once `lightd` is live)

| What | Why |
|---|---|
| `wled_engine/layer.py`, `compositor.py`, `blend.py`, `fleet/`, `core.py`, `daemon.py`, `adapters/` | Replaced by `lightd` |
| `wled_app/`: `ui/`, `visualizer/`, `storage/`, `engine/`, `domain/`, `protocols/drgb.py`, `protocols/warls.py`, `patterns/`, `__main__.py` | Old terminal app and single-board types; useful pieces moved |
| `run.py`, `wled`, `run-stack` | Old CLI entry points; replace with `python -m lightd` |
| Tests for the layer stack, ASCII visualizer and terminal app | Go with their code |

`tests/test_controller_config.py` and `tests/test_reload_identify.py` carry over as tests for `wled`/`lightd`.

## Phases

1. **`wled`**: discovery, HTTP client, DDP, `WledController` (channels from `/json/cfg`, `send_channels`, identify),
   with unit tests.
2. **Lua runtime**: `lupa` sandbox, instruction budget, prelude, schema extraction, per-channel byte rendering. Port
   the stateless patterns. Benchmark 810 px at 30 fps.
3. **`lightd`**: config, registry with discovery, targets, runs (replace, persist), loop, REST API, `__main__`.
4. **Web**: move `/config` onto `lightd`; build the pattern editor (CodeMirror, wasmoon preview, run on target, save).
5. **Switch over**: Dockerfiles/compose run `lightd`; README and DEPLOYMENT updated.
6. **Delete** the retired code.

## Running it (dev)

```sh
PYTHONPATH=src python -m lightd --controllers config/controllers.json --port 8765
python sequencer.py --web --engine http://127.0.0.1:8765      # then open /patterns and /config
```

`--dry-run` renders without sending UDP; `--no-discovery` skips mDNS.
