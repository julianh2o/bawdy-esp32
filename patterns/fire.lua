-- Flickering fire made from layered noise: hot at pixel 0, cooling along the strip.
params = {
  speed = { type = "number", default = 1.5, min = 0, max = 10, step = 0.1 },
  height = { type = "number", default = 0.6, min = 0.05, max = 1, step = 0.01, label = "Flame height (share of strip)" },
}

function pixel(n, t, p)
  local x = n / COUNT
  local flicker = noise(n * 0.15, t * p.speed * 3) * 0.6 + noise(n * 0.05, t * p.speed) * 0.4
  local heat = clamp((1 - x / p.height) * 1.2 + (flicker - 0.5) * 0.8)
  -- black -> red -> orange -> yellow -> white
  return clamp(heat * 3), clamp(heat * 3 - 1), clamp(heat * 3 - 2)
end
