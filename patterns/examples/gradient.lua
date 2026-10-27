-- Two-color gradient that slides along the strip.
params = {
  from = { type = "color", default = "#ff3c00" },
  to = { type = "color", default = "#7800ff" },
  speed = { type = "number", default = 0.1, min = -1, max = 1, step = 0.01 },
}

function pixel(n, t, p)
  local k = (n / COUNT - t * p.speed) % 1
  k = 1 - math.abs(k * 2 - 1) -- there and back, so it wraps without a seam
  return mix(p.from.r, p.to.r, k), mix(p.from.g, p.to.g, k), mix(p.from.b, p.to.b, k)
end
