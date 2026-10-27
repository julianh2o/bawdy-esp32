-- Larson scanner: an eye that sweeps back and forth.
params = {
  color = { type = "color", default = "#ff0000" },
  period = { type = "number", default = 2, min = 0.3, max = 10, step = 0.1, label = "Seconds per round trip" },
  width = { type = "number", default = 8, min = 1, max = 40, step = 1 },
}

function pixel(n, t, p)
  local phase = (t / p.period) % 1
  local pos = (1 - math.abs(phase * 2 - 1)) * (COUNT - 1)
  local k = clamp(1 - math.abs(n - pos) / p.width)
  k = k * k
  return p.color.r * k, p.color.g * k, p.color.b * k
end
