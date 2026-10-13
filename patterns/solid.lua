-- One color on every pixel.
params = {
  color = { type = "color", default = "#ff8000" },
  brightness = { type = "number", default = 1, min = 0, max = 1, step = 0.01 },
}

function pixel(n, t, p)
  local k = p.brightness
  return p.color.r * k, p.color.g * k, p.color.b * k
end
