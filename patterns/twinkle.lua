-- Random pixels sparkle and fade.
params = {
  color = { type = "color", default = "#ffe0a0" },
  density = { type = "number", default = 0.15, min = 0, max = 1, step = 0.01, label = "Share of pixels lit" },
  rate = { type = "number", default = 1, min = 0.05, max = 10, step = 0.05, label = "Twinkles per second" },
  background = { type = "number", default = 0.02, min = 0, max = 1, step = 0.01 },
}

function pixel(n, t, p)
  -- each pixel gets its own phase offset, so twinkles don't line up
  local cycle = t * p.rate + hash(n, 7)
  local slot = math.floor(cycle)
  local k = p.background
  if hash(n, slot) < p.density then
    local f = cycle - slot
    k = math.max(k, math.sin(f * math.pi) ^ 3)
  end
  return p.color.r * k, p.color.g * k, p.color.b * k
end
