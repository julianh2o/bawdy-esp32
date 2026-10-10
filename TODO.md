# TODO

Improvement backlog from the initial code review. Each item is sized to fit in one PR.
Already filed as issues: repo hygiene cleanup, Dockerize the daemon.

## High priority (before on-site hardware testing)

### 1. Send plain RGB over DDP and leave color order to WLED (bug)
The engine reorders colors to the patch's `color_order` (GRB) before sending (`src/wled_engine/spatial/sampler.py`). WLED expects RGB over DDP and does its own reordering per LED output, so colors get reordered twice and red and green swap on real hardware. The simulator undoes the engine's swap, so the tests can't catch it.
- Always send RGB. Keep `color_order` only as information, or use it to check against the board's config.
- Update the simulator's decoding (`unpermute_color` in `patch_table.py`) to match.
- Test: `#ff0000` produces packet bytes `ff 00 00`.

### 2. Label RGBW data correctly in DDP packets (bug)
RGBW segments are packed at 4 bytes per pixel, but `DdpEmitter` always sets data type `0x01` (RGB), so WLED reads them as 3 bytes per pixel.
- Set the DDP data type for 4 channels per pixel when the board's output is RGBW.
- Calculate the per-packet split in bytes, not LEDs.
- Test: an RGBW fixture split across several packets.

### 3. Make the frame sync optional and add a hardware test (hardening)
The engine holds every board's frame (push flag cleared) and then broadcasts one empty "show now" packet. This hasn't been verified on real WLED.
- Add a setting to send every frame immediately instead (push flag set on each frame's last packet) as a fallback.
- Add `run.py probe --ip X` to send a known test pattern to one board, with and without the sync.
- Add an on-site testing checklist to `docs/`.

### 4. Let the simulator mimic the real network setup (testing)
Real boards each have their own IP and all listen on port 4048. The simulator puts all boards on one machine with different ports (4048+i), so the broadcast sync isn't really exercised.
- Add a mode where each virtual board gets its own local IP (127.0.0.x) on port 4048.
- Integration test for the broadcast sync in this mode.

## Infrastructure

### 6. Run tests on GitHub automatically
- GitHub Actions: Python tests on 3.10 and 3.12, and `mvn test` for `sequencer-java`.
- Run on every PR and on pushes to `main`.

### 7. Lock down the REST API (security)
- Remove `Access-Control-Allow-Origin: *`, or replace it with an allowlist.
- Optional access token (env var / CLI flag).
- Keep `127.0.0.1` as the default address; anything else must be set explicitly.
- Prerequisite for exposing the Docker daemon on the network.

### 8. Lower CPU use and make frame timing measurable (performance)
- The busy-wait in `FrameClock.sleep_until_next_frame` uses about 35% of a core. Make it optional or shorten it.
- Report frame-time stats (p50/p99, dropped frames) through `/api/status`.
- Speed up the per-pixel packing in `SpatialSampler` by building whole rows of bytes at once.
- Benchmark before and after at 30 fps on the 20-node venue.

## Docs

### 9. Tone down the README
- Replace the marketing language with what's actually tested and what isn't.
- Document the hardware setup: board IPs, DDP on port 4048, color order configured on the board.
