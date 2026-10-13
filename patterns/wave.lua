-- Sine wave of brightness traveling along the strip.
params = {
  color = { type = "color", default = "#3060ff" },
  wavelength = { type = "number", default = 40, min = 2, max = 1000, label = "Pixels per wave" },
  speed = { type = "number", default = 30, min = -600, max = 600, label = "Pixels per second" },
}

function pixel(n, t, p)
  local k = (math.sin((n - t * p.speed) / p.wavelength * 2 * math.pi) + 1) / 2
  return p.color.r * k, p.color.g * k, p.color.b * k
end
