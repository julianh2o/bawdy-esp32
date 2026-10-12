# WLED Multi-Node Lighting Architecture & Universe Manager

An enterprise-grade, modular lighting choreography and spatial infrastructure management platform designed for real-world multi-controller venue installations (20+ hardwired Cat6 WLED ESP32 boards, overhead festoon strings, matrix panels, floor lamps, and stage trusses) as well as single-strip setups.

The platform provides a **3-tier layered architecture**: a high-throughput **virtual hardware digital twin simulator**, a **headless 10-layer compositing engine and REST daemon**, and a native **Java 17 Swing desktop Universe Manager** with concurrent timeline cue scheduling, 2D spatial CAD venue visualization, live hardware patch editing, and controller fleet telemetry.

---

## 🏗️ System Architecture & Layered Design

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                   TIER 3: DESKTOP UNIVERSE MANAGER [Java 17 Swing]               │
│                                                                                  │
│   ┌────────────────────────┐  ┌─────────────────────────┐  ┌─────────────────┐   │
│   │  Timeline & Cue Editor │  │  Spatial Stage Universe  │  │ Controller Fleet│   │
│   │  • Layers 0..9 Cues    │  │  • 2D CAD Venue Canvas   │  │   & Patch Table │   │
│   │  • Concurrent Timing   │  │  • Fixture Hierarchy     │  │  • 20 Cat6 Nodes│   │
│   │  • Time Dilation Scrubber││  • 3D Coordinates / Waypoints│ • 31 Segments   │   │
│   └───────────┬────────────┘  └────────────┬────────────┘  └────────┬────────┘   │
└───────────────┼────────────────────────────┼────────────────────────┼────────────┘
                │ HTTP REST API (:8765)      │ Local Preview          │ Patch Sync
┌───────────────▼────────────────────────────▼────────────────────────▼────────────┐
│                  TIER 2: HEADLESS LIGHTING ENGINE & DAEMON [Python]              │
│                                                                                  │
│   ┌──────────────────────────────────────────────────────────────────────────┐   │
│   │  10-Layer Compositor (Layers 0..9) & Mathematical Blend Modes            │   │
│   │  (Overwrite, Alpha Blend, Additive, Multiply, Max, Mask)                 │   │
│   ├──────────────────────────────────────────────────────────────────────────┤   │
│   │  Continuous 3D Spatial Field Sampler (Point3D -> Fixtures -> Patch)      │   │
│   ├──────────────────────────────────────────────────────────────────────────┤   │
│   │  MultiNodeDispatcher: Zero-Allocation UDP / DDP Buffer Packing           │   │
│   │  Cat6 Broadcast Frame Sync (0x41) Clock Latching                         │   │
│   └────────────────────────────────────┬─────────────────────────────────────┘   │
└────────────────────────────────────────┼─────────────────────────────────────────┘
                                         │ UDP / DDP (Ports 4048..4067)
┌────────────────────────────────────────▼─────────────────────────────────────────┐
│               TIER 1: HARDWARE & DIGITAL TWIN SIMULATOR LAYER                    │
│                                                                                  │
│   ┌────────────────────────────────────────┐  ┌──────────────────────────────┐   │
│   │  20 Virtual WLED ESP32 Controllers     │  │  HTML5 / Canvas Visualizer   │   │
│   │  • Independent UDP Sockets (4048..4067)│  │  • http://localhost:8080    │   │
│   │  • State Machine & Stream Integrity HUD│  │  • Real-Time Spatial Venue   │   │
│   └───────────────────┬────────────────────┘  └──────────────▲───────────────┘   │
│                       └───────────── WebSocket ──────────────┘                   │
│                                                                                  │
│   (OR Physical Cat6 Network: 20x Athom / ESP32 WLED Hardware Boards)             │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### How the Layers Interact

1. **Tier 1 — Hardware & Digital Twin Simulation (`wled_simulator`):**
   - In simulation mode, runs 20 virtual WLED ESP32 controllers on individual UDP ports (`4048` through `4067`).
   - Processes realtime lighting protocols (**DDP**, **DRGB**, **DNRGB**, **WARLS**) through real operating system sockets.
   - Listens for the DDP `0x41` Broadcast Frame Synchronization packet on `255.255.255.255:4048` to latch all 20 boards simultaneously without inter-controller frame tearing.
   - Streams aggregated fixture state over WebSockets to an interactive CAD web visualizer at `http://localhost:8080`.
   - In real-world production, this layer is replaced with physical ESP32 boards wired via Cat6 cables to an Ethernet switch.

2. **Tier 2 — Headless Lighting Engine Daemon (`wled_engine`):**
   - Operates a deterministic 30 FPS monotonic frame clock loop (`FrameClock`).
   - Manages **10 independent compositing layers** (`Layer 0` to `Layer 9`), calculating non-linear cosine opacity transitions, color blending, and channel masks.
   - Evaluates continuous 3D mathematical fields (`SpatialSampler`) and maps them to physical fixtures through the [`PatchTable`](data/venue/patch.json).
   - Serializes contiguous byte buffers for each controller and broadcasts UDP packets via [`MultiNodeDispatcher`](src/wled_engine/fleet/dispatcher.py).
   - Exposes a complete JSON REST API on port `8765` for real-time remote orchestration.
   - Enforces deterministic 30.00 FPS pacing with microsecond spin-waiting and zero-copy DDP datagram encoding.

3. **Tier 3 — Desktop Universe Manager (`sequencer-java`):**
   - Native Java 17 Swing application built for authoring, hardware patch validation, and live execution.
   - Communicates with the Python engine daemon over HTTP REST.
   - Provides three specialized workspaces: **Timeline & Cues**, **Spatial Stage Universe**, and **Controller Fleet & Hardware Patch Table**.
   - Features a dedicated **Universe** menu for managing spatial fixtures, controller fleets, patch tables, and infrastructure validation.

---

## 🚀 Quick Start

### Requirements
- **Python 3.10+** (with `rich` and `prompt_toolkit` for the terminal visualizer)
- **Java 17+** and **Maven** (for the native desktop Universe Manager)

### 1. Launch the Full Stack (One Command)
To launch all three layers simultaneously with automatic health monitoring and background process management:

```bash
./run-stack
```

This starts:
1. The **20-Node Virtual Hardware Simulator** (Ports `4048..4067`, Web UI at `http://localhost:8080`)
2. The **Lighting Engine REST Daemon** (HTTP API at `http://127.0.0.1:8765`)
3. The **Java Universe Manager GUI** (Desktop Swing Window)

Pressing <kbd>Ctrl+C</kbd> in your terminal gracefully shuts down all services.

### 2. Launch Individual Services
Each layer can also be run independently in separate terminal windows:

```bash
# Terminal 1: 20-Node Hardware Digital Twin Simulator (Web UI: http://localhost:8080)
python3 sim.py --venue                        # Default: Metro Concert Hall (20 nodes)
python3 sim.py --preset warehouse_rave        # Or specify any preset stage

# Terminal 2: Headless Lighting Engine Daemon (REST API: http://127.0.0.1:8765)
python3 run.py daemon --venue                 # Default: Metro Concert Hall (20 nodes)
python3 run.py daemon --preset warehouse_rave # Or specify any preset stage

# Terminal 3: Java Desktop Universe Manager
./sequencer-gui
```

---

## 🏛️ Built-In Venue Stages & Preset Universes

The platform includes 4 production-grade venue stages out of the box, complete with realistic 3D Cartesian coordinates, fixture zones (groups), Cat6 controller node assignments, and 0-collision hardware patch tables:

| Preset Stage | Code ID | Controllers | Fixtures | Pixels | Zones / Groups | Highlights |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Metro Concert Hall & Lounge** | `concert_hall` | 20 nodes (`wled_01..20`) | 28 | 5,153 | `trusses`, `festoon`, `panels`, `lamps`, `perimeter`, `projectors`, `stage`, `bar`, `ceiling` | Structural truss box, 4 catenary festoon bulb strings, 3x 16×16 matrices, floor accent lamps, projector surface. |
| **Warehouse Rave & Boiler Stage** | `warehouse_rave` | 16 nodes (`wled_01..16`) | 26 | 4,197 | `dj_cage`, `strobes`, `matrices`, `totems`, `perimeter`, `overhead_beams`, `projectors` | 360° central DJ cage with 4 corner uprights, 4 high-output strobe blinders, dual 16×16 industrial matrix walls, 4 ground totems, diagonal overhead cross-beams. |
| **Outdoor Amphitheater & Lawn** | `festival_amphitheater` | 16 nodes (`wled_01..16`) | 24 | 3,805 | `proscenium`, `canopy`, `stage_deck`, `imag_screens`, `delay_towers`, `foh_booth`, `vip_garden`, `projectors` | Grand curved proscenium arch, 6 swooping festoon strings reaching deep into the lawn, center catwalk runway, dual 16×16 IMAG screens, 4 audio delay towers, FOH sound booth surround. |
| **Immersive Art Gallery & Studio** | `art_gallery` | 12 nodes (`wled_01..12`) | 22 | 2,821 | `ceiling_halos`, `wall_blades`, `sculpture`, `floor_tracks`, `museum_spots`, `projectors` | 3 concentric floating hexagonal ceiling halos, 8 vertical wall light blades, centerpiece sculpture pedestal with 16×16 interactive top matrix and base glow, 4 floor runners, museum accent spots. |

### Switching Presets

1. **Desktop Universe Manager (Java):**
   - Click the **Stage Preset** dropdown directly in the top toolbar of the **Spatial Stage Universe** tab, or
   - Select **Universe → Load Preset Stage & Universe → [Preset Name]** in the main menu bar.
   - The CAD canvas automatically auto-fits (`Fit Stage`), the Fixture tree updates, and Controller Fleet and Hardware Patch tables are reloaded instantly.
2. **REST API Daemon:**
   - Query all available presets: `GET /api/universe/presets`
   - Switch active stage live: `POST /api/universe` with payload `{"preset": "warehouse_rave"}`
3. **CLI Arguments:**
   - `python3 sim.py --preset art_gallery`
   - `python3 run.py daemon --preset festival_amphitheater`

---

## 🎛️ WLED Universe Manager [Java 17 Swing]

The **WLED Universe Manager** (`sequencer-java/`) is a native desktop studio designed for venue infrastructure management, real-time spatial inspection, and temporal show choreography without browser DOM or CSS limitations.

Built with clean, standard Swing components and the **Arial** font throughout (no third-party styling bugs, missing letters, or emoji rendering glitches), it provides three dedicated views:

### 1. Timeline & Cue Editor Workspace
* **Concurrent Multi-Layer Timeline:** Schedule multiple simultaneous cues across **Layers 0 through 9**. Unlike simple sequential chasers, multiple layers can run concurrently at the exact same moment.
* **Two-Way Timing Synchronization:** Two-way calculated inputs for Start Time, Duration, and Stop Time (`Stop = Start + Duration`; modifying Stop automatically updates Duration).
* **Section-Based Show Organization:** Cues are arranged by named musical/lighting sections (`Intro`, `Main Drive`, `Chorus`, `Bridge`, `Outro`).
* **Dynamic Property Inspector:** Tabbed panel (`General & Layer`, `Pattern & Color`, `Timing & Fade`) with instant two-way synchronization.
* **Transport Controls & Monospace HUD:** `Play`, `Pause`, `Stop`, `Prev`, `Next`, interactive click-to-seek scrubber bar, and live elapsed digital readout with active cue counter.
* **Time Dilation (Speed Scaling):** Slider scaling playback from `0.1x` (slow-motion) to `4.0x` (high speed) without altering sequence timing data.
* **Flexible Looping Modes:** `Loop Infinitely`, `Play N Times`, `Play Once & Hold`, and `Play Once & Blackout`.
* **Immediate Blackout:** Dedicated **Blackout** button that immediately halts playback, resets the stage view, and flushes an all-black frame across all hardware controllers.

### 2. Spatial Stage Universe Editor
* **Interactive 2D CAD Stage View:** Real-time top-down canvas showing the entire venue layout:
  - 4x Stage Trusses (span and uprights)
  - 2x Perimeter Wall Ambient Strips
  - 4x Overhead Festoon Bulb Strings with catenary cable sag
  - 3x 16x16 High-Density Matrix Panels
  - 2x Architectural Floor Lamps
  - Stage Projector Screen with projection beam throw cone
* **Camera Navigation:** Pan (drag background), Zoom (scroll wheel or `+ Zoom` / `- Zoom`), `Fit Stage` auto-frame, and `1:1 Reset`.
* **Live FX Mode Previews:** Preview effects directly on the canvas (`Timeline Cues`, `45 Deg Angle Sweep`, `Radial Pulse`, `Rainbow Cloud`, `Linear Gradient`, `Blackout All`).
* **Fixture Hierarchy Tree & Details Inspector:** Browse fixtures grouped by category (`trusses`, `festoon`, `panels`, `lamps`, `perimeter`, `projectors`), with two-way selection linking between tree and canvas.
* **Full Fixture Editing:**
  - **Add Fixture:** Create new fixtures with custom ID, Name, Group, Type, Pixel Count, Color Order, and 3D Waypoints (Start & End X, Y, Z).
  - **Edit Fixture:** Modify physical coordinates or configuration of any existing fixture.
  - **Delete Fixture:** Remove fixtures with confirmation dialog.
  - **Save Universe:** Persist all universe modifications directly to [`data/venue/universe.json`](data/venue/universe.json).

### 3. Controller Fleet & Hardware Patch Table Editor
* **Controller Fleet Management (20 Cat6 Nodes):**
  - Monitors all hardwired WLED controller nodes, showing Node ID, Role/Name, IP Address, Port, Protocol, Assigned Fixtures, LED Count, Latency, and Sync Status.
  - **In-place Table Editing:** Directly edit controller Names, IP addresses, and UDP ports in table cells.
  - **Action Buttons:** **Add Controller**, **Edit Controller**, and **Delete Controller**.
* **Hardware Patch Table Editor (31 Segments):**
  - Maps fixture segments to controller channel indices (CH0..CH3), start offsets, pixel counts, and color orders (`GRB`, `RGB`).
  - **In-place Table Editing:** Double-click cells to modify Fixture ID, Controller Node, Channel, Start Offset, Count, Color Order, and Wiring Direction.
  - **Action Buttons:** **Add Segment**, **Edit Segment**, and **Delete Segment**.
  - **Save Patch Table:** Write updates directly to [`data/venue/patch.json`](data/venue/patch.json).
* **Fleet Network Utilities:**
  - **Ping Fleet:** Network health sweep verifying controller availability.
  - **Broadcast Sync (0x41):** Manual trigger emitting the DDP frame sync packet to latch all nodes simultaneously.
  - **Validate Patch:** Verification tool checking for channel collisions or port overlaps.
  - **Blackout Fleet:** Broadcasts an all-black frame to all 20 controller nodes.

---

## ⚡ Headless Lighting Engine (`wled_engine`)

The core lighting engine is decoupled from any UI framework, allowing Python scripts, background daemons, Home Assistant bridges, or external controllers to drive multi-layer lighting logic.

### Key Capabilities

* **10 Indexed Compositing Layers (`Layer 0` to `Layer 9`):** Rendered in strict bottom-to-top order.
* **Mathematical Blend Modes:**
  * `BlendMode.OVERWRITE`: Direct pixel replacement.
  * `BlendMode.ALPHA_BLEND`: Standard transparency compositing ($C_{out} = C_{top} \times \alpha + C_{bottom} \times (1 - \alpha)$).
  * `BlendMode.ADDITIVE`: Additive luminance blending with 255 clamping.
  * `BlendMode.MULTIPLY`: Shading, darkener, and vignette masking.
  * `BlendMode.MAX`: Peak component selector.
  * `BlendMode.MASK`: Luminance-based alpha masking.
* **Smooth Cosine Opacity Tweens:** Non-linear ease-in-out curves for smooth fade-ins, fade-outs, and crossfades.
* **Continuous 3D Spatial Field Sampling:** Evaluates functions in 3D Cartesian space (`f(Point3D) -> Color`) and samples exact coordinates for each fixture.
* **Atomic Blackout & Flush:** Atomically clears active patterns, resets layers, and transmits an all-black frame to all controllers with frame sync.

### Python API Example

```python
from wled_engine import LightingEngine, BlendMode
from wled_engine.spatial.venue import create_demo_venue
from wled_engine.spatial.patterns import spatial_radial_pulse
from wled_engine.spatial.coordinates import Point3D
from wled_app.domain.color import Color

# 1. Initialize engine and configure 20-node venue universe
universe, patch_table = create_demo_venue()
engine = LightingEngine(target_fps=30.0, dry_run=False)
engine.setup_universe(universe, patch_table)

# 2. Start the background 30 FPS clock loop
engine.start()

# 3. Trigger continuous 3D spatial pulse radiating from DJ booth (0, 5.5, 0)
engine.set_spatial_pattern(
    spatial_radial_pulse(
        center=Point3D(0.0, 5.5, 0.0),
        speed=2.0,
        frequency=1.5,
        color_center=Color(255, 220, 50),
        color_edge=Color(10, 20, 80),
    )
)

# 4. Immediate blackout and stop
# engine.blackout()
# engine.stop()
```

---

## 🌐 REST API Daemon Reference

The engine includes a built-in HTTP REST API daemon (`EngineDaemon`) on port `8765`. For full JSON payload schemas, see the [REST API Reference](docs/API_GUIDE.md).

### Summary of Routes

| Method | Route | Description |
| :--- | :--- | :--- |
| `GET` | `/api/status` | Engine lifecycle, actual FPS, tick, master brightness, and fleet status |
| `POST` | `/api/start` | Start the real-time frame clock loop |
| `POST` | `/api/stop` | Stop the real-time frame clock loop (flushes blackout) |
| `POST` | `/api/blackout` | Immediately reset all layers, stop sequences, and flush black frame to all hardware |
| `GET` | `/api/layers` | List all 10 compositing layers (Layers 0..9) |
| `POST` | `/api/layers/{0..9}` | Configure pattern, color, speed, blend mode, or channels on a layer |
| `POST` | `/api/layers/{0..9}/fade` | Trigger non-linear cosine opacity transition |
| `POST` | `/api/sequence/play` | Play sequence timeline (with optional `time_dilation`) |
| `POST` | `/api/sequence/pause` | Pause sequence timeline playback |
| `POST` | `/api/sequence/stop` | Stop sequence playback, reset timeline to 0.0, and flush blackout |
| `POST` | `/api/sequence/seek` | Seek sequence timeline to specific timestamp (`{"time": 12.5}`) |
| `POST` | `/api/spatial/pattern` | Set continuous 3D spatial field (`angle_sweep`, `radial_pulse`, `rainbow`, `gradient`, `off`) |
| `GET` | `/api/fleet` | Retrieve Cat6 telemetry (frames sent, bytes, latency) across all controllers |
| `POST` | `/api/master_brightness` | Adjust global dimmer without losing relative layer balance |
| `POST` | `/api/cues` | Snapshot all 10 layers as a named cue |
| `POST` | `/api/sync` | Emit broadcast frame sync datagram (0x41) across Cat6 network |
| `GET` | `/api/universe/presets` | List catalog of available venue presets with fixture, pixel, and zone metadata |
| `POST` | `/api/universe` | Switch active venue universe (`{"preset": "warehouse_rave"}`) |
| `GET` | `/api/patch` | Retrieve active hardware patch table and collision validation |
| `GET` | `/api/config` | Active controller endpoints, unassigned controllers, and boards being identified |
| `POST` | `/api/config/reload` | Re-read universe, patch, and controllers files and apply them live (previous config kept on error) |
| `POST` | `/api/identify` | Blink a board white (`{"ip": "10.0.0.5", "led_count": 300}` or `{"controller_id": "wled_01"}`; `"led_start": 270` blinks only LEDs from there on, e.g. one output; `"stop": true` ends it) |
| `POST` | `/api/events` | Publish an event to the reactive rule engine |

### Example Curl Commands

```bash
# Check engine status and FPS
curl http://127.0.0.1:8765/api/status

# Set Layer 0 to flowing rainbow pattern
curl -X POST http://127.0.0.1:8765/api/layers/0 \
  -H "Content-Type: application/json" \
  -d '{"pattern": "rainbow", "speed": 1.5, "opacity": 1.0, "enabled": true}'

# Smoothly fade Layer 0 to 0% over 2 seconds
curl -X POST http://127.0.0.1:8765/api/layers/0/fade \
  -H "Content-Type: application/json" \
  -d '{"target_opacity": 0.0, "duration_sec": 2.0}'

# Trigger continuous 3D radial pulse across universe
curl -X POST http://127.0.0.1:8765/api/spatial/pattern \
  -H "Content-Type: application/json" \
  -d '{"pattern": "radial_pulse", "speed": 1.5, "frequency": 2.0, "color_center": "#FFD700", "color_edge": "#000033"}'

# Immediate Blackout across all controllers
curl -X POST http://127.0.0.1:8765/api/blackout
```

---

## 🖥️ Simulated UDP Device (Hardware Digital Twin)

For testing and visual verification without needing physical LED hardware powered on, the repository includes a **WLED ESP32 Hardware Digital Twin Simulator** (`sim.py`).

### Launching the Simulator

```bash
# Full 20-Node Venue Universe Simulation (Ports 4048..4067, Web UI http://localhost:8080):
python3 sim.py --venue

# Single Controller Profile Simulation (e.g. 3 channels of 270 LEDs on port 4048):
python3 sim.py --channels 270 270 270 --port 4048
```

### Web Visualizer Features (`http://localhost:8080`)
* **Real-time Spatial Rendering:** Visualizes all 28 fixtures across the 13m x 13.5m venue.
* **Stream Integrity HUD:** Monitors FPS, Packets/Sec, Throughput, and Jitter.
* **Interactive Controls:** Smooth Pan, Zoom, and Auto-Fit to window.

---

## 💻 Interactive Terminal CLI & ASCII Visualizer

The package also includes an interactive terminal UI for single-device inspection:

```bash
# Launch interactive terminal wizard:
python3 -m wled_app
# Or:
wled-app
```

### Terminal CLI Commands
```bash
# Create and save a device profile
wled-app create --name "LivingRoom" --ip 192.168.1.150 --channels 60 30 144 0 --protocol ddp

# List saved devices
wled-app list

# Stream real-time rainbow with ASCII visualizer
wled-app play --device "LivingRoom" --pattern rainbow --fps 30

# Test pattern in preview dry-run mode (no network packets sent)
wled-app play --device "LivingRoom" --pattern chase --color "#FF0000" --dry-run
```

---

## 🔌 Adding Custom Patterns

Implementing a new visual effect requires implementing the `Pattern` protocol:

```python
from wled_app.domain.color import Color
from wled_app.domain.device import DeviceConfig
from wled_app.domain.frame import FrameBuffer
from wled_app.patterns.base import PatternConfig

class SparklePattern:
    @property
    def id(self) -> str:
        return "sparkle"

    @property
    def name(self) -> str:
        return "Random Sparkle"

    @property
    def description(self) -> str:
        return "Random LED sparkle across active channels"

    def render(self, tick: int, device: DeviceConfig, frame: FrameBuffer, config: PatternConfig) -> None:
        frame.clear()
        import random
        for ch in device.active_channels:
            if ch.length > 0:
                random_pixel = random.randint(0, ch.length - 1)
                frame.set_pixel(ch.start_index + random_pixel, config.primary_color)
```

Register it in [`src/wled_app/patterns/registry.py`](src/wled_app/patterns/registry.py) to make it immediately accessible across the CLI, REST daemon, and desktop sequencer!

---

## 🧪 Testing & Verification

The codebase includes exhaustive test suites covering protocol serialization, compositing math, spatial sampling, and multi-node network dispatch:

### Run Python Tests (129 tests)
```bash
PYTHONPATH=src python3 -m unittest discover tests
```

### Run Java Unit Tests (15 tests)
```bash
cd sequencer-java && mvn -o test
```

### Rebuild Standalone Java Executable JAR
```bash
./sequencer-java/build.sh
```
Produces `sequencer-java/target/wled-sequencer.jar`.
