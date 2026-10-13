-- Hard on/off flashing.
params = {
  color = { type = "color", default = "#ffffff" },
  rate = { type = "number", default = 4, min = 0.1, max = 30, step = 0.1, label = "Flashes per second" },
  duty = { type = "number", default = 0.2, min = 0.01, max = 0.99, step = 0.01, label = "Share of time on" },
}

function pixel(n, t, p)
  if (t * p.rate) % 1 < p.duty then return p.color.r, p.color.g, p.color.b end
  return 0, 0, 0
end
