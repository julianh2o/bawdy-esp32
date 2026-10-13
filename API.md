# Driving the lights (for agents)

`lightd` runs Lua patterns on WLED boards. You control it over a JSON REST API at
**`https://lights.julianverse.net/engine/api`**. The web UI is at `https://lights.julianverse.net` (pattern editor; setup at `/config`).

## Concepts

- **Target**: `controller/channel`, 1-based, e.g. `wled_01/2`. A channel is a WLED segment. A Govee light's RGB and
  white segments are one linked channel (e.g. `govee_01/1`). List them with `GET /controllers`.
- **Pattern**: Lua defining `pixel(n, t, p)` that returns `r, g, b[, w]` in 0..1. `n` is 0..`COUNT-1`, `t` is Unix
  time in seconds, `p` holds params. Built-ins: `solid`, `white`, `rainbow`, `chase`, `breathe`, `fire`, `gradient`,
  `scanner`, `strobe`, `twinkle`, `wave`. Leave `w` out and white LEDs are used automatically.
- **Run**: a pattern + params on one or more targets. Starting a run on a channel replaces whatever ran there.
- **Scene**: a saved set of runs, activated by name.
- **API value**: a key/value pair stored in lightd (in memory) and set over REST, e.g. live audio levels. Patterns
  read them through `type = "api"` params. See [Live values](#live-values-api-params).
- **Output switch**: when off, nothing is sent and the boards show their own WLED effects.

## Common calls

```sh
API=https://lights.julianverse.net/engine/api

curl $API/status                      # fps, boards online, runs, errors
curl $API/controllers                 # boards, channels (target, name, length, linked, rgbw), brightness
curl -X POST $API/controllers/refresh  # re-read every board's segments and pixel counts now (else every 30 s)
curl $API/patterns                    # library with each pattern's params schema
curl $API/runs                        # what's playing
curl "$API/logs?level=warning&limit=100"   # recent log (also ?since=<seq>, ?format=text); web view at /logs

# Play a library pattern (params are optional; unknown ones fall back to defaults)
curl -X POST $API/runs -H 'Content-Type: application/json' \
  -d '{"targets": ["wled_01/1", "wled_01/2"], "pattern": "solid", "params": {"color": "#ff4000"}}'

curl -X PATCH  $API/runs/<id> -d '{"params": {"speed": 0.5}}'   # change params live
curl -X DELETE $API/runs/<id>                                   # stop one run (its channels go black)
curl -X POST   $API/blackout                                    # stop everything

curl -X PUT $API/brightness -d '{"targets": ["wled_01/3"], "brightness": 0.4}'   # per channel, 0..1
curl -X PUT $API/output -d '{"enabled": false}'                                   # hand back to WLED

curl $API/scenes
curl -X POST $API/scenes/party/activate
curl -X PUT  $API/scenes/cozy -d '{"entries": [{"targets": ["wled_01/1"], "pattern": "white", "params": {"kelvin": 2400}, "brightness": 0.6}]}'

curl -X POST $API/patterns/check -d '{"source": "..."}'   # compile only; returns params schema or an error with line
curl -X PUT  $API/patterns/my_pattern -d '{"source": "..."}'   # save to the library
curl -X POST $API/identify -d '{"target": "wled_01/2"}'        # blink a channel to find it
curl -X PUT $API/values/audio.level -d '{"value": 0.8}'        # set a live value for api params
```

Errors come back as a non-2xx status with `{"status": "error", "message": "...", "line": n}`. `line` is only
there for Lua errors.

## Posting Lua directly

You don't have to save a pattern to play it. `POST /runs` takes `source` (Lua code) in place of `pattern`, and the
code runs on the targets straight away. Check it first with `/patterns/check`, which compiles it and returns its
params schema, or the error with its line number. Keeping the Lua in a file and letting `jq` build the JSON
avoids quoting trouble:

```sh
cat > /tmp/sunset.lua <<'LUA'
params = { speed = { type = "number", default = 0.05 } }
function pixel(n, t, p)
  local k = n / COUNT
  return hsv(0.08 - 0.1 * k + 0.02 * math.sin(t * p.speed * 6.283), 0.9, 1 - 0.6 * k)
end
LUA

jq -Rs '{source: .}' /tmp/sunset.lua | curl -X POST $API/patterns/check -d @-          # compile only
jq -Rs '{targets: ["wled_01/1", "govee_01/1"], source: ., params: {speed: 0.1}}' /tmp/sunset.lua \
  | curl -X POST $API/runs -d @-                                                        # play it now
jq -Rs '{source: .}' /tmp/sunset.lua | curl -X PUT $API/patterns/sunset -d @-          # save it as "sunset"
```

- An ad-hoc run shows as "editor code" in the UI. It survives a lightd restart, but it can't be put in a scene until
  it's saved to the library.
- To change the code, post it again to the same targets; the new run replaces the old one. `PATCH /runs/<id>` only
  changes params.
- If a frame fails (a Lua error, or the instruction budget runs out), the channel keeps its last good frame and the
  error shows in `GET /runs` and `GET /status`.

## Writing a pattern

```lua
params = {
  color = { type = "color", default = "#00c8ff" },
  speed = { type = "number", default = 1, min = 0, max = 5 },   -- min/max are slider hints only
}

function pixel(n, t, p)
  local k = (math.sin(n / COUNT * 6.283 - t * p.speed) + 1) / 2
  return p.color.r * k, p.color.g * k, p.color.b * k
end
```

- Helpers: `hsv(h, s, v)`, `mix(a, b, k)`, `clamp(x, lo, hi)`, `smoothstep(x)`, `noise(x, y)`, `hash(n, seed)`,
  `color("#rrggbb")`, plus `math`.
- Patterns are pure: there's no state between frames. Use `t`, plus `hash` or `noise` for randomness.
- No `io`, `os`, `require` or `load`. Each frame has an instruction budget, so avoid heavy loops.
- `WHITE` is true when the channel has white LEDs. Return `w` yourself only to take control of white.

## Live values (api params)

A param declared with `type = "api"` isn't set by run params or the editor's Params panel (it doesn't show there).
lightd fills it every frame from a key/value store you write over REST, so an outside program (an audio analyser, a
sensor, a MIDI bridge) can drive patterns live without restarting runs:

```lua
params = {
  level = { type = "api", default = 0 },                -- reads key "level" (the param's name)
  bands = { type = "api", key = "audio.bands" },        -- reads key "audio.bands"
  color = { type = "color", default = "#ff2060" },      -- ordinary param, still in the Params panel
}

function pixel(n, t, p)
  local band = p.bands and p.bands[1 + math.floor(n / COUNT * #p.bands)] or 0
  local k = math.max(p.level, band)
  return p.color.r * k, p.color.g * k, p.color.b * k
end
```

- `key` defaults to the param's name. Several patterns (and runs) can read the same key.
- A value can be any JSON: number, string, boolean, array, object. Arrays arrive as 1-based Lua tables, objects
  as tables with string keys. lightd passes it through as-is (no type coercion or range check), so guard for the
  shape you expect.
- A key that isn't set gives the param's `default` (or `nil` if it has none), so always handle that case.
- New values reach the LEDs on the next frame (30 fps by default). The editor's preview polls them about 10 times a
  second.
- Values live in memory only: a lightd restart clears them. A key keeps its last value until it's changed or
  deleted, so a sender that stops should clear its keys (or send zeros) if the lights shouldn't freeze.
- Keys are 1-64 characters from letters, digits, `_`, `.`, `:` and `-` (use dots to group, e.g. `audio.bass`). At
  most 1000 keys.

```sh
curl $API/values                     # {"keys": [...], "values": {k: v}, "updated": {k: unix time}, "listening": [...]}
                                     # listening = keys the playing runs read, i.e. what's worth sending
curl $API/values/audio.level         # {"key": "audio.level", "value": 0.8, "updated": 1760000000.1}; 404 if unset

curl -X PUT    $API/values/audio.level -d '{"value": 0.8}'               # set one key
curl -X PATCH  $API/values -d '{"values": {"audio.level": 0.8, "audio.bands": [0.9, 0.4, 0.1]}}'
                                                                          # set several at once; null deletes a key
curl -X PUT    $API/values -d '{"values": {"audio.level": 0}}'           # replace everything with exactly this
curl -X DELETE $API/values/audio.level                                    # remove one key (404 if unset)
curl -X DELETE $API/values                                                # remove every key
```

Writes answer `{"status": "ok", ...}` with the new value(s). For a high-rate feed (audio at 30-60 updates a second)
send everything in one `PATCH` per update, and reuse the HTTP connection (keep-alive) rather than opening one per
request. Bodies are limited to 256 KB.

More detail: `REFACTOR.md` (design and full endpoint list).
