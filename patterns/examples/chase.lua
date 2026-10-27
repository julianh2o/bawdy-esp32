-- A lit block running along the strip with a fading tail.
params = {
  color = { type = "color", default = "#00c8ff" },
  speed = { type = "number", default = 60, min = -200, max = 200, label = "Pixels per second" },
  width = { type = "number", default = 12, min = 1, max = 60, step = 1, label = "Length in pixels" },
}

function pixel(n, t, p)
  local head = (t * p.speed) % COUNT
  local d = (head - n) % COUNT
  if d >= p.width then return 0, 0, 0 end
  local k = 1 - d / p.width
  return p.color.r * k, p.color.g * k, p.color.b * k
end
