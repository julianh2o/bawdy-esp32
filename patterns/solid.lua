-- One color on every pixel.
params = {
  color = { type = "color", default = "#ff8000" },
}

function pixel(n, t, p)
  return p.color.r, p.color.g, p.color.b
end
