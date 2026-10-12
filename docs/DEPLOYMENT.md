# Deployment (Docker)

Two images, published to Docker Hub and run together by `docker-compose.yaml`:

| Service / container | Image | Dockerfile | Purpose |
|---|---|---|---|
| `daemon` / `bawdy-lights-daemon` | `julianh2o/bawdy-lights-daemon` | `Dockerfile.daemon` | Lighting engine. REST API on port 8765, DDP to the WLED boards. |
| `web` / `bawdy-lights-web` | `julianh2o/bawdy-lights-web` | `Dockerfile.web` | Browser sequencer on port 8080. Stores sequences and proxies `/engine/*` to the daemon. |
| `sim` / `bawdy-lights-sim` (profile) | `bawdy-lights-sim` (local build only) | `Dockerfile.daemon --target sim` | Virtual boards + visualizer, for testing without hardware. |

## 1. Prepare the network and boards

- Give every board a **fixed IP** (DHCP reservation on the router, or a static IP in WLED under *Config → WiFi Setup*). The engine addresses boards by IP, so a changed address means a dark board.
- In WLED, enable realtime DDP: *Config → Sync Interfaces → Realtime* → "Receive UDP realtime" on, port **4048** (the default). Make sure "Use main segment only" and timeouts are left at defaults unless you know you need them.
- Set the **LED color order on the board** (*Config → LED Preferences*). The engine sends plain data; don't compensate in the patch. (See TODO.md #1: the engine still applies the patch's `color_order`; keep these consistent until that's fixed.)
- Boards and the Docker host must be on the same subnet, since the frame-sync packet is a UDP broadcast.

## 2. Configure

```sh
cp .env.example .env
cp config/universe.json.example   config/universe.json
cp config/patch.json.example      config/patch.json
cp config/controllers.json.example config/controllers.json
```

Then adopt your boards, either in the web UI or by hand. The board-to-fixture mapping lives in `patch.json`, which is edited by hand for now.

**Web UI (`http://<docker-host>:8080/config`)** lists the adopted controllers. Each one expands into its channels: the board's LED outputs, with GPIO pins and LED counts read from the board. Give each channel a name there. Below the list, a collapsed "N unadopted boards discovered" section shows boards found on the LAN that aren't adopted yet; adopting one opens its channels, ready to name. **Identify** blinks LEDs white for a few seconds so you can tell which physical strip is which: on a board it blinks every LED, on a channel only that output's LEDs. It works before adopting, and falls back to sending directly from the web container if the daemon is down.

**Save & apply** writes `config/controllers.json` and tells the daemon to reload it, so changes take effect within a frame.

- Discovery uses mDNS: every WLED board advertises `_wled._tcp`, named by its mDNS address (*Config → WiFi Setup*). mDNS is multicast and doesn't cross subnets or VLANs, so boards elsewhere can be added with **Add by IP**, which reads `http://<ip>/json/info`. **Rescan** queries the network again right away.
- Adopting stores the board's MAC address. If the board later shows up at a different IP, the page offers to update the IP.
- Channel names are stored per controller, in output order: `"channels": [{"name": "Front truss"}, {"name": "Back wall"}]`.
- If `patch.json` gives `"channel_lengths": {"wled_01": [270, 270, 270]}`, the engine pads each channel to that length. It sends a board's channels back to back in one DDP stream, so without them a partly mapped channel would shift every later channel onto the wrong output. They must match the board's LED outputs (*LED Preferences*).
- The web container runs as uid 10001 and needs write access to the file:
  ```sh
  cp config/controllers.json.example config/controllers.json   # or: echo '{}' > config/controllers.json
  chmod a+w config/controllers.json
  ```
  Without that, the page is read-only and says why.

**By hand:** edit `config/controllers.json`:

```json
{ "wled_01": { "ip": "192.168.1.101", "port": 4048 } }
```

`port` defaults to 4048. Extra keys (the page stores `name`, `mac`, `host` and `channels`) are ignored by the daemon. You can instead embed the same mapping as a top-level `"controllers"` key in `patch.json`; entries in `controllers.json` override it. A controller in the patch with no IP gets no output; the daemon starts anyway and lists it in a warning, so a fresh install can be configured from the web UI.

After editing config files by hand, apply them without a restart:

```sh
curl -X POST http://<docker-host>:8765/api/config/reload
```

This re-reads `controllers.json` and the universe/patch files. If anything is invalid, the daemon keeps running the previous config and returns the error.

Set `WLED_BROADCAST_IP` in `.env` to your subnet broadcast (e.g. `192.168.1.255`). The default `255.255.255.255` works on many networks but some routers/OSes drop it.

Config is mounted read-only at `/config`; it is not baked into the image. The built-in presets (`data/presets`) are in the image.

## 3. Run

```sh
docker compose pull         # julianh2o/bawdy-lights-daemon + julianh2o/bawdy-lights-web from Docker Hub
docker compose up -d        # or: docker compose up -d --build   (build from this checkout)
docker compose ps           # both should become "healthy"
curl http://<docker-host>:8765/api/status   # "running": true, actual_fps near WLED_FPS
```

- Web UI: `http://<docker-host>:8080` from any machine on the LAN.
- Daemon API: `http://<docker-host>:8765`. Point the Java Universe Manager here (`ENGINE_URL=http://<docker-host>:8765 ./sequencer-gui`).

Pin a version with `IMAGE_TAG=<version>` in `.env` (default `latest`).

### Networking

Both containers use `network_mode: host`:

- The daemon needs it so the DDP sync broadcast reaches the LAN; Docker's bridge network does not forward broadcasts.
- By default **both** the web UI (`WEB_PORT`, 8080) and the daemon API (`DAEMON_PORT`, 8765) are reachable on the LAN, so the Java Universe Manager and scripts can talk to the daemon directly.
- The daemon API has **no authentication** (see TODO.md #7). Anyone on the network can control the lights. To lock it down, set `DAEMON_API_HOST=127.0.0.1` in `.env`: the API is then only reachable on the Docker host, and the web UI keeps working through its `/engine/*` proxy.
- Host networking is Linux-only (not Docker Desktop on macOS/Windows). With host networking, `ports:` is ignored: change `WEB_PORT` / `DAEMON_PORT` in `.env`.

### Simulator (no hardware)

```sh
# .env: WLED_SIMULATOR=true
docker compose --profile sim up --build
```

The visualizer is at `http://<host>:8090`. The daemon sends to `127.0.0.1` ports 4048+n, which the `sim` container listens on.

### Sequences

Saved sequences live in the `sequences` named volume (`/data/sequences`) and survive `docker compose down && up`. `docker compose down -v` deletes them.

### Updating

```sh
docker compose pull && docker compose up -d     # published images
# or, from source:
git pull && docker compose up -d --build
```

On `docker stop`/`compose down` the daemon receives SIGTERM, stops the engine, and sends a blackout frame before exiting (`stop_grace_period: 10s`).

## Building and publishing images

`scripts/docker-build.sh` builds both images with the version, build date and git revision as OCI labels. It refuses to run with uncommitted changes.

```sh
./scripts/docker-build.sh                    # build both, load locally (linux/amd64)
./scripts/docker-build.sh --push             # build both and push to Docker Hub
./scripts/docker-build.sh --push --version 0.2.0
./scripts/docker-build.sh --only daemon      # just one image (daemon|web)
PLATFORM=linux/amd64,linux/arm64 ./scripts/docker-build.sh --push   # multi-arch, e.g. for a Pi
```

Each build is tagged `julianh2o/<image>:<version>` and `:latest`. The version defaults to `git describe --tags --always`. Pushing needs `docker login` as `julianh2o`. Override the namespace with `REGISTRY_USER=...`.

## Environment reference

Compose (`.env`): `REGISTRY_USER`, `IMAGE_TAG`, `WEB_PORT`, `DAEMON_PORT`, `DAEMON_API_HOST`, `SIM_WEB_PORT`, plus the daemon variables below.

Daemon: `WLED_API_HOST`, `WLED_API_PORT`, `WLED_FPS`, `WLED_UNIVERSE`, `WLED_PATCH`, `WLED_CONTROLLERS`, `WLED_PRESET`, `WLED_BROADCAST_IP`, `WLED_SIMULATOR`, `WLED_DRY_RUN` (each maps to the CLI flag of the same name). Web: `SEQUENCER_HOST`, `SEQUENCER_PORT`, `SEQUENCES_DIR`, `ENGINE_URL`.

## Not covered

The Java GUI is not containerized. Image builds are manual (`scripts/docker-build.sh`); CI publishing is not set up (see TODO.md #6).
