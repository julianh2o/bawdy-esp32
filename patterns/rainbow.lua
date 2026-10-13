-- Rainbow that scrolls along the strip.
params = {
  speed = { type = "number", default = 0.2, min = -5, max = 5, step = 0.05, label = "Cycles per second" },
  repeats = { type = "number", default = 1, min = 0.1, max = 20, step = 0.1, label = "Rainbows along the strip" },
  saturation = { type = "number", default = 1, min = 0, max = 1, step = 0.01 },
}

function pixel(n, t, p)
  return hsv(n / COUNT * p.repeats - t * p.speed, p.saturation, 1)
end
