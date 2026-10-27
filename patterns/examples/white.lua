-- White light at a color temperature, from warm candlelight to cool daylight.
-- On LEDs with a white chip, the white chip does as much of the work as it can and RGB tints it to the
-- temperature asked for, so set white_led to your white chip's temperature.
params = {
  kelvin = { type = "number", default = 2700, min = 1800, max = 6500, step = 50, label = "Color temperature (K)" },
  white_led = { type = "number", default = 4000, min = 2000, max = 6500, step = 100, label = "White chip temperature (K)" },
}

-- Black-body color for a temperature (Tanner Helland's fit, good from 1000 K to 40000 K), r/g/b in 0..1.
local function temperature(kelvin)
  local t = clamp(kelvin, 1000, 40000) / 100
  local r, g, b
  if t <= 66 then
    r = 255
    g = 99.4708025861 * math.log(t) - 161.1195681661
  else
    r = 329.698727446 * (t - 60) ^ -0.1332047592
    g = 288.1221695283 * (t - 60) ^ -0.0755148492
  end
  if t >= 66 then
    b = 255
  elseif t <= 19 then
    b = 0
  else
    b = 138.5177312231 * math.log(t - 10) - 305.0447927307
  end
  return clamp(r / 255), clamp(g / 255), clamp(b / 255)
end

function pixel(n, t, p)
  local r, g, b = temperature(p.kelvin)
  if not WHITE then return r, g, b end
  -- As much white chip as fits under the target color, then RGB for the rest.
  local wr, wg, wb = temperature(p.white_led)
  local w = math.min(1, r / wr, wg > 0 and g / wg or 1, wb > 0 and b / wb or 1)
  return r - w * wr, g - w * wg, b - w * wb, w
end
