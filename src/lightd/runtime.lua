-- Pattern runtime, shared by lightd (Python, via lupa) and the browser preview (wasmoon).
-- Lua 5.4. Keep it free of host-specific code so a pattern renders the same in both.
--
-- A pattern is Lua source that defines:
--   params = { speed = { type = "number", default = 1, min = 0, max = 5 }, ... }   -- optional
--   function pixel(n, t, p) return r, g, b end   -- n: 0..COUNT-1, t: Unix time in seconds, r/g/b: 0..1

local M = {}

M.budget = 2000000 -- Lua instructions allowed per render call (one channel, one frame)

local floor, concat, char, format = math.floor, table.concat, string.char, string.format
local sethook = debug.sethook

-- ---------- helpers available to patterns ----------

local function clamp(x, lo, hi)
  lo, hi = lo or 0, hi or 1
  if x < lo then return lo elseif x > hi then return hi end
  return x
end

local function mix(a, b, k)
  return a + (b - a) * k
end

local function smoothstep(t)
  return t * t * (3 - 2 * t)
end

-- h, s, v in 0..1 -> r, g, b in 0..1
local function hsv(h, s, v)
  s, v = s or 1, v or 1
  h = (h % 1) * 6
  local i = floor(h)
  local f = h - i
  local p, q, u = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
  if i == 0 then return v, u, p
  elseif i == 1 then return q, v, p
  elseif i == 2 then return p, v, u
  elseif i == 3 then return p, q, v
  elseif i == 4 then return u, p, v
  end
  return v, p, q
end

-- Deterministic pseudo-random value in [0, 1) for an integer (and optional seed).
local function hash(n, seed)
  local x = (floor(n) * 374761393 + floor(seed or 0) * 668265263) & 0xffffffff
  x = ((x ~ (x >> 13)) * 1274126177) & 0xffffffff
  x = x ~ (x >> 16)
  return x / 4294967296.0
end

-- Smooth value noise in [0, 1) over x (and optional y), e.g. noise(n * 0.1, t).
local function noise(x, y)
  y = y or 0
  local ix, iy = floor(x), floor(y)
  local ux, uy = smoothstep(x - ix), smoothstep(y - iy)
  local function at(a, b) return hash(a * 73856093 ~ b * 19349663) end
  return mix(mix(at(ix, iy), at(ix + 1, iy), ux), mix(at(ix, iy + 1), at(ix + 1, iy + 1), ux), uy)
end

-- "#ff8000" -> {r = 1, g = 0.5, b = 0}
local function color(hex)
  if type(hex) == "table" then return hex end
  local r, g, b = tostring(hex or ""):match("^#?(%x%x)(%x%x)(%x%x)$")
  if not r then return { r = 0, g = 0, b = 0 } end
  return { r = tonumber(r, 16) / 255, g = tonumber(g, 16) / 255, b = tonumber(b, 16) / 255 }
end

M.helpers = {
  clamp = clamp, mix = mix, smoothstep = smoothstep, hsv = hsv, hash = hash, noise = noise, color = color,
}

-- ---------- sandbox ----------

local SAFE_GLOBALS = {
  "assert", "error", "ipairs", "next", "pairs", "pcall", "select", "tonumber", "tostring", "type",
  "xpcall", "rawequal", "rawget", "rawlen", "rawset", "setmetatable", "getmetatable",
}

local function copy(t)
  local out = {}
  for k, v in pairs(t) do out[k] = v end
  return out
end

local function budgeted(f, ...)
  sethook(function() error("frame budget exceeded (endless loop?)", 0) end, "", M.budget)
  local res = table.pack(pcall(f, ...))
  sethook()
  return table.unpack(res, 1, res.n)
end

-- Compile pattern source. Returns env, or nil and an error message ("pattern:LINE: ...").
function M.compile(source)
  local env = {}
  for _, k in ipairs(SAFE_GLOBALS) do env[k] = _G[k] end
  env.math, env.string, env.table, env.utf8 = copy(math), copy(string), copy(table), copy(utf8)
  env.string.dump = nil
  env.print = function() end
  for k, v in pairs(M.helpers) do env[k] = v end
  env.COUNT = 1

  local chunk, err = load(source, "=pattern", "t", env)
  if not chunk then return nil, err end
  local ok, e = budgeted(chunk)
  if not ok then return nil, tostring(e) end
  if type(env.pixel) ~= "function" then
    return nil, "pattern must define function pixel(n, t, p)"
  end
  return env
end

-- ---------- params ----------

local function infer_type(default)
  local ty = type(default)
  if ty == "number" or ty == "boolean" then return ty end
  if ty == "string" and default:match("^#%x%x%x%x%x%x$") then return "color" end
  return "string"
end

-- Normalized params schema: { name = {type, default, min, max, step, options, label} }.
-- min/max set the UI slider's range; they don't limit the value, which can be typed in beyond them.
function M.schema(env)
  local out = {}
  if type(env.params) ~= "table" then return out end
  for name, spec in pairs(env.params) do
    if type(name) == "string" then
      if type(spec) ~= "table" then spec = { default = spec } end
      out[name] = {
        type = spec.type or infer_type(spec.default),
        default = spec.default,
        min = spec.min, max = spec.max, step = spec.step,
        options = spec.options, label = spec.label,
      }
    end
  end
  return out
end

local function coerce(s, v)
  if s.type == "number" then
    return tonumber(v) or tonumber(s.default) or 0
  elseif s.type == "boolean" then
    return v == true or v == "true" or v == 1
  elseif s.type == "color" then
    return color(v)
  elseif s.type == "choice" then
    if type(s.options) == "table" then
      for _, o in ipairs(s.options) do if o == v then return v end end
    end
    return s.default
  end
  return v
end

-- The p table a pattern sees: schema defaults, overridden by values; extra values pass through.
function M.params(env, values)
  local p = {}
  for name, s in pairs(M.schema(env)) do
    local v = values and values[name]
    if v == nil then v = s.default end
    p[name] = coerce(s, v)
  end
  if values then
    for k, v in pairs(values) do
      if p[k] == nil then p[k] = v end
    end
  end
  return p
end

-- ---------- rendering ----------

local function to_byte(v)
  if type(v) ~= "number" then error("pixel() must return three numbers r, g, b (0..1)", 0) end
  if v ~= v or v <= 0 then return 0 elseif v >= 1 then return 255 end
  return floor(v * 255 + 0.5)
end

local function hex3(r, g, b) return format("%02x%02x%02x", r, g, b) end

local function loop(env, count, t, p, reversed, fmt)
  env.COUNT = count
  local pixel = env.pixel
  local out = {}
  for i = 0, count - 1 do
    local n = reversed and (count - 1 - i) or i
    local r, g, b = pixel(n, t, p)
    out[i + 1] = fmt(to_byte(r), to_byte(g), to_byte(b))
  end
  return concat(out)
end

-- One channel's frame as a byte string (3 bytes per pixel): returns true, data or false, error.
function M.render(env, count, t, p, reversed)
  return budgeted(loop, env, count, t, p, reversed, char)
end

-- Same, as a hex string ("rrggbb" per pixel); used by the browser, which can't take binary strings.
function M.render_hex(env, count, t, p, reversed)
  return budgeted(loop, env, count, t, p, reversed, hex3)
end

return M
