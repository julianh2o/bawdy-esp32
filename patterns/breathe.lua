-- Whole strip fades in and out.
params = {
  color = { type = "color", default = "#ff2060" },
  period = { type = "number", default = 4, min = 0.5, max = 15, step = 0.1, label = "Seconds per breath" },
  floor = { type = "number", default = 0.05, min = 0, max = 1, step = 0.01, label = "Dimmest level" },
}

function pixel(n, t, p)
  local k = (1 - math.cos(t / p.period * 2 * math.pi)) / 2
  k = p.floor + (1 - p.floor) * k * k
  return p.color.r * k, p.color.g * k, p.color.b * k
end
