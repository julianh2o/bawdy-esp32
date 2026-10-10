# Deployment (Docker)

Two images, run together by `docker-compose.yaml`:

| Service | Dockerfile | Purpose |
|---|---|---|
| `daemon` | `Dockerfile.daemon` | Lighting engine. REST API on `127.0.0.1:8765`, DDP to the WLED boards. |
| `web` | `Dockerfile.web` | Browser sequencer on port 8080. Stores sequences and proxies `/engine/*` to the daemon. |
| `sim` (profile) | `Dockerfile.daemon --target sim` | Virtual boards + visualizer, for testing without hardware. |

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

Edit `config/controllers.json` to map each controller id in `patch.json` to its board:

```json
{ "wled_01": { "ip": "192.168.1.101", "port": 4048 } }
```

`port` defaults to 4048. You can instead embed the same mapping as a top-level `"controllers"` key in `patch.json`; entries in `controllers.json` override it. The daemon refuses to start if a controller has no IP (unless `--simulator`/`--dry-run`).

Set `WLED_BROADCAST_IP` in `.env` to your subnet broadcast (e.g. `192.168.1.255`). The default `255.255.255.255` works on many networks but some routers/OSes drop it.

Config is mounted read-only at `/config`; it is not baked into the image. The built-in presets (`data/presets`) are in the image.

## 3. Run

```sh
docker compose up -d --build
docker compose ps          # both should become "healthy"
curl http://127.0.0.1:8765/api/status     # "running": true, actual_fps near WLED_FPS
```

Open `http://<docker-host>:8080` from any machine on the LAN.

### Networking

Both containers use `network_mode: host`:

- The daemon needs it so the DDP sync broadcast reaches the LAN; Docker's bridge network does not forward broadcasts.
- The daemon API has no authentication (see TODO.md #7), so it binds `127.0.0.1`. The web container shares the host network to reach it, and the browser only ever talks to the web UI (`/engine/*` is proxied). Only the web port is exposed.
- Host networking is Linux-only (not Docker Desktop on macOS/Windows). With host networking, `ports:` is ignored: change `WEB_PORT` in `.env`.

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
git pull
docker compose up -d --build
```

On `docker stop`/`compose down` the daemon receives SIGTERM, stops the engine, and sends a blackout frame before exiting (`stop_grace_period: 10s`).

## Environment reference

Daemon: `WLED_API_HOST`, `WLED_API_PORT`, `WLED_FPS`, `WLED_UNIVERSE`, `WLED_PATCH`, `WLED_CONTROLLERS`, `WLED_PRESET`, `WLED_BROADCAST_IP`, `WLED_SIMULATOR`, `WLED_DRY_RUN` (each maps to the CLI flag of the same name). Web: `SEQUENCER_HOST`, `SEQUENCER_PORT`, `SEQUENCES_DIR`, `ENGINE_URL`.

## Not covered

The Java GUI is not containerized. The CI image build is not set up (needs a workflow file; see TODO.md #6).
