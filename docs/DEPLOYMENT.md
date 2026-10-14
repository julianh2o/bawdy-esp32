# Deployment (Docker)

Two images, published to Docker Hub and run together by `docker-compose.yaml`:

| Service / container | Image | Dockerfile | Purpose |
|---|---|---|---|
| `daemon` / `bawdy-lights-daemon` | `julianh2o/bawdy-lights-daemon` | `Dockerfile.daemon` | **lightd**: runs Lua patterns on WLED channels, owns the configuration. REST API on port 8765, DDP to the boards. |
| `web` / `bawdy-lights-web` | `julianh2o/bawdy-lights-web` | `Dockerfile.web` | Browser UI on port 8080: the pattern editor (runs and scenes) at `/`, setup at `/config` (the gear icon). Proxies `/engine/*` to lightd. |
| `sim` / `bawdy-lights-sim` (profile) | `bawdy-lights-sim` (local build only) | `Dockerfile.daemon --target sim` | Parked: the old universe/patch simulator. Doesn't work with lightd. |

## 1. Prepare the network and boards

- Prefer a **fixed IP** per board (DHCP reservation, or a static IP in WLED under *Config → WiFi Setup*). lightd follows a board that changes IP when mDNS sees it (matched by MAC), but a fixed address is the reliable path.
- In WLED, enable realtime DDP: *Config → Sync Interfaces → Realtime* → "Receive UDP realtime" on, port **4048** (the default).
- Set each output's **LED count and color order on the board** (*Config → LED Preferences*). lightd reads the outputs from the board and always sends RGB; the board reorders colors itself.
- Boards on the Docker host's subnet are discovered automatically; boards elsewhere can be added by IP.

## 2. Configure

```sh
cp .env.example .env
mkdir -p config
sudo chown -R 10001 config    # lightd runs as uid 10001 and writes its configuration here
```

lightd owns the configuration in `config/`:

| File | What | Written by |
|---|---|---|
| `controllers.json` | Adopted boards: id, IP, port, MAC/host, channel names and `reversed` flags | `/config` page (via lightd), or by hand |
| `patterns/*.lua` | Your saved patterns | Pattern editor (via lightd) |
| `scenes.json` | Scenes: named sets of patterns with params on channels | Pattern editor, "Save as scene" (via lightd) |
| `lightd-state.json` | Patterns currently running and channel brightness, restored on restart | lightd |

**Web UI (`http://<docker-host>:8080/config`)** lists the adopted controllers. Each one expands into its channels: the board's LED outputs, with GPIO pins and LED counts as lightd reads them from the board. Name each channel and tick **Reversed** if pixel 0 is at the far end. Below the list, a collapsed "N unadopted boards discovered" section shows boards lightd found on the LAN that aren't adopted yet. **Identify** blinks LEDs white for a few seconds so you can tell which physical strip is which: on a board it blinks every LED, on a channel only that output's LEDs.

**Save & apply** sends the configuration to lightd (`PUT /api/config`), which validates it, writes `config/controllers.json` and applies it at once. Every call the page makes goes to lightd's API through the web server's `/engine/*` proxy; the web container has no access to `config/`.

- Discovery uses mDNS: every WLED board advertises `_wled._tcp`, named by its mDNS address (*Config → WiFi Setup*). mDNS is multicast and doesn't cross subnets or VLANs, so boards elsewhere can be added with **Add by IP**. **Rescan** queries the network again right away.
- Adopting stores the board's MAC address and mDNS host. lightd follows the board if its IP changes; the page shows the new address and offers to save it.
- Channel lengths aren't stored: lightd reads them from the board, so the board's *LED Preferences* are the source of truth.

**By hand:** edit `config/controllers.json`:

```json
{
  "wled_01": {
    "ip": "192.168.1.101",
    "port": 4048,
    "channels": [{ "name": "Front truss" }, { "name": "Back wall", "reversed": true }]
  }
}
```

`port` defaults to 4048. Apply it without a restart:

```sh
curl -X POST http://<docker-host>:8765/api/config/reload
```

If the file is invalid, lightd keeps the previous configuration and returns the error.

### Configuration API (lightd)

| Method | Path | |
|---|---|---|
| GET | `/api/config` | `controllers.json` as stored, with its path, whether lightd can write it, and any read error |
| PUT | `/api/config` | `{"controllers": {...}}`: validate, save and apply |
| POST | `/api/config/reload` | re-read the file from disk |
| GET | `/api/controllers` | live state per controller: online, current IP, board info, channels |
| GET | `/api/discovered` | boards seen over mDNS (and probed by IP) |
| POST | `/api/discovered/rescan`, `/api/discovered/probe` | rescan now; probe `{"ip": ...}` |
| POST | `/api/identify` | `{"target": "wled_01/2"}`, `{"controller_id": ...}` or `{"ip": ..., "led_start", "led_count"}`; `"stop": true` ends it |

## 3. Run

```sh
docker compose pull         # julianh2o/bawdy-lights-daemon + julianh2o/bawdy-lights-web from Docker Hub
docker compose up -d        # or: docker compose up -d --build   (build from this checkout)
docker compose ps           # both should become "healthy"
curl http://<docker-host>:8765/api/status   # actual_fps near LIGHTD_FPS
```

- Web UI: `http://<docker-host>:8080` from any machine on the LAN.
- lightd API: `http://<docker-host>:8765`. (The Java sequencer still speaks the old engine API; see REFACTOR.md.)

Pin a version with `IMAGE_TAG=<version>` in `.env` (default `latest`).

### Networking

Both containers use `network_mode: host`:

- lightd needs it for mDNS discovery of WLED boards; multicast doesn't cross Docker's bridge network.
- By default **both** the web UI (`WEB_PORT`, 8080) and the daemon API (`DAEMON_PORT`, 8765) are reachable on the LAN, so the Java Universe Manager and scripts can talk to the daemon directly.
- The daemon API has **no authentication** (see TODO.md #7). Anyone on the network can control the lights. To lock it down, set `DAEMON_API_HOST=127.0.0.1` in `.env`: the API is then only reachable on the Docker host, and the web UI keeps working through its `/engine/*` proxy.
- Host networking is Linux-only (not Docker Desktop on macOS/Windows). With host networking, `ports:` is ignored: change `WEB_PORT` / `DAEMON_PORT` in `.env`.

### Simulator (parked)

The `sim` profile runs the old universe/patch simulator, which pairs with the old engine daemon (`run.py daemon --simulator`), not lightd. To try lightd without hardware, run it with `LIGHTD_DRY_RUN=true`: it renders frames but sends nothing.

### Updating

```sh
docker compose pull && docker compose up -d     # published images
# or, from source:
git pull && docker compose up -d --build
```

On `docker stop`/`compose down` lightd receives SIGTERM, stops rendering, and sends a blackout frame before exiting (`stop_grace_period: 10s`). Running patterns are restored when it starts again.

## Building and publishing images

**Automatic:** every push to `main` (except Markdown-only changes) runs `.github/workflows/deploy.yml`. It runs the
Python tests, builds and pushes both images to Docker Hub as `:latest` and `:sha-<commit>`, then restarts the `lights`
stack on ceto (`/opt/lights`, compose file from fairgrounds-infra's `templates/lights-compose.yml.j2`). It uses
fairgrounds-infra's shared `docker-deploy.yml` workflow and its secrets (`DOCKERHUB_*`, `TS_OAUTH_*`,
`DEPLOY_SSH_KEY`). Run it by hand from the Actions tab with "Run workflow".

**By hand:** `scripts/docker-build.sh` builds both images with the version, build date and git revision as OCI labels. It refuses to run with uncommitted changes.

```sh
./scripts/docker-build.sh                    # build both, load locally (linux/amd64)
./scripts/docker-build.sh --push             # build both and push to Docker Hub
./scripts/docker-build.sh --push --version 0.2.0
./scripts/docker-build.sh --only daemon      # just one image (daemon|web)
PLATFORM=linux/amd64,linux/arm64 ./scripts/docker-build.sh --push   # multi-arch, e.g. for a Pi
```

Each build is tagged `julianh2o/<image>:<version>` and `:latest`. The version defaults to `git describe --tags --always`. Pushing needs `docker login` as `julianh2o`. Override the namespace with `REGISTRY_USER=...`.

## Pattern assistant (optional)

The editor has a chat panel under the code where an LLM writes and edits patterns. It's off until the **web**
container gets a model. Any OpenAI-compatible chat API works: set the variables in `.env`.

```sh
# Local Ollama (e.g. kvasir): no key
ASSISTANT_BASE_URL=http://10.10.0.40:11434/v1
ASSISTANT_MODEL=qwen3-8-64k:latest
# DeepSeek's API
ASSISTANT_BASE_URL=https://api.deepseek.com
ASSISTANT_MODEL=deepseek-flash
ASSISTANT_API_KEY=sk-...
```

The browser only talks to the web server (`/api/assistant/chat`), which holds the key and calls the model. Every code
block in a reply is test-rendered in the browser with lightd's own Lua runtime. If it fails, the error is sent back to
the model for a fix, up to twice. Nothing reaches the LEDs until you press Run.

Model choice: the model needs to be good at writing code and to fit **entirely** in the GPU's memory. When part of
it spills into system RAM, replies slow down. On kvasir's 24 GB card, Qwen3.8 27B (`qwen3-8-64k`) passed every test
prompt. Small models (around 8B) write code that doesn't run.

**Turn its thinking down.** Qwen3.8 thinks at its maximum level by default, which made replies take 20-180 s.
`ASSISTANT_REASONING_EFFORT` sets `reasoning_effort` on each request; Ollama honours it, and `think: false` is
ignored. Measured on the 5 test prompts, every result correct:

| Effort | Reply time | Notes |
|---|---|---|
| default (max) | 19-180 s | |
| `low` | 7-20 s | Recommended: a little planning for trickier requests |
| `none` | 4-12 s | No thinking at all |

The OpenAI-compatible endpoint can't set the context length, so Ollama loads the model at its own default (64k for
`qwen3-8-64k`). That spills about 1.4 GB into system RAM and costs about 25% speed (78 vs 105 tokens/s at 16k). A
copy with a 16k context, which is plenty for the assistant's ~4k-token requests, fits entirely on the GPU:

```sh
curl http://10.10.0.40:11434/api/create -d '{"model": "qwen-lights", "from": "qwen3-8-64k:latest", "parameters": {"num_ctx": 16384}}'
# then ASSISTANT_MODEL=qwen-lights
```

## Environment reference

Compose (`.env`): `REGISTRY_USER`, `IMAGE_TAG`, `WEB_PORT`, `DAEMON_PORT`, `DAEMON_API_HOST`, `LIGHTD_FPS`, `LIGHTD_DRY_RUN`, `SIM_WEB_PORT`,
`ASSISTANT_BASE_URL`, `ASSISTANT_MODEL`, `ASSISTANT_API_KEY`, `ASSISTANT_REASONING_EFFORT`.

lightd: `LIGHTD_HOST`, `LIGHTD_PORT`, `LIGHTD_FPS`, `LIGHTD_DRY_RUN`, `CONTROLLERS_FILE`, `USER_PATTERNS_DIR`, `LIGHTD_STATE`, `BUILTIN_PATTERNS_DIR`, `DISCOVERY` (`false` turns mDNS off). Web: `SEQUENCER_HOST`, `SEQUENCER_PORT`, `SEQUENCES_DIR`, `ENGINE_URL`, `ASSISTANT_BASE_URL`, `ASSISTANT_MODEL`,
`ASSISTANT_API_KEY`, `ASSISTANT_TEMPERATURE` (default 0.3), `ASSISTANT_TIMEOUT` (seconds per reply, default 600), `ASSISTANT_REASONING_EFFORT` (`none`/`low`/`medium`/`high`).

## Not covered

The Java GUI is not containerized. CI runs the Python tests before deploying, but not on pull requests, and doesn't run the Java tests (see TODO.md #6).
