"""CRT scanlines and light noise over everything.

One set of numbers, kept as ``"video_effects"`` in main_config.json, drives
two renderers:

* the player hands mpv a small GLSL user shader (``--glsl-shaders``) that
  darkens every Nth output line and sprinkles per-frame noise - this runs on
  the GPU at output resolution, so it costs next to nothing and the lines
  are screen pixels, not video pixels;
* the menu draws the same lines and noise on a transparent layer above its
  pages (``fs42.menu.app``).

Values:

    scanline_opacity    0..1    how dark the lines are (0 = off)
    scanline_size       2..16   line pitch: one line every N screen pixels
    scanline_thickness  1..8    how many pixels each line covers
    scanline_style      soft | medium | hard   the edge of each line
    scanline_pattern    horizontal | vertical | grid
    noise_opacity       0..1    how strong the grain is (0 = off)
    noise_grain         1..4    grain size in screen pixels

The shape of a line is defined once, in ``line_profile``, and the shader
is generated from the same numbers, so the menu and the video match.
"""

import json
import logging
import os

from fs42 import paths

_l = logging.getLogger("VFX")

DEFAULTS = {
    "scanline_opacity": 0.0,
    "scanline_size": 3,
    "scanline_thickness": 1,
    "scanline_style": "hard",
    "scanline_pattern": "horizontal",
    "noise_opacity": 0.0,
    "noise_grain": 2,
}
LIMITS = {
    "scanline_opacity": (0.0, 1.0),
    "scanline_size": (2, 16),
    "scanline_thickness": (1, 8),
    "noise_opacity": (0.0, 1.0),
    "noise_grain": (1, 4),
}
CHOICES = {
    "scanline_style": ["soft", "medium", "hard"],
    "scanline_pattern": ["horizontal", "vertical", "grid"],
}
CONFIG_KEY = "video_effects"


def clean(values) -> dict:
    """A complete, clamped settings dict from whatever was given."""
    out = dict(DEFAULTS)
    if isinstance(values, dict):
        for key, (low, high) in LIMITS.items():
            if key in values:
                try:
                    number = float(values[key])
                except (TypeError, ValueError):
                    continue
                number = max(low, min(high, number))
                out[key] = int(round(number)) if isinstance(DEFAULTS[key], int) else round(number, 3)
        for key, options in CHOICES.items():
            value = str(values.get(key, "")).strip().lower()
            if value in options:
                out[key] = value
    # A line must leave some picture between it and the next one.
    out["scanline_size"] = min(LIMITS["scanline_size"][1], max(out["scanline_size"], out["scanline_thickness"] + 1))
    return out


# ---------------------------------------------------------------- shape

def _edges(values):
    """(pitch, centre, lo, hi, smooth) for one line, in screen pixels.

    Darkness at distance d from a line's centre is 1 - ramp(d) where ramp
    goes 0 -> 1 between lo and hi: a step for "hard", a straight ramp for
    "medium", an S-curve over a wider band for "soft".
    """
    v = clean(values)
    t = float(v["scanline_thickness"])
    pitch = float(v["scanline_size"])
    half = t / 2.0
    style = v["scanline_style"]
    if style == "hard":
        lo, hi = half - 0.001, half + 0.001
    elif style == "medium":
        # Solid core, a one-pixel shoulder either side.
        lo, hi = half - 0.25, half + 1.0
    else:
        # A beam: fades in from the middle and out over a wide band.
        lo, hi = half * 0.25 - 0.25, half + 1.0 + 0.5 * t
    # Never let the fade run past the middle of the gap, or the gap itself
    # gets tinted and the lines stop reading as lines.
    hi = min(hi, pitch / 2.0)
    lo = min(lo, hi - 0.25)
    return pitch, half, lo, hi, style == "soft"


def line_profile(values):
    """Darkness 0..1 of each pixel across one period of the line pattern."""
    pitch, centre, lo, hi, smooth = _edges(values)
    out = []
    for p in range(int(pitch)):
        q = (p + 0.5 - centre) % pitch
        d = min(q, pitch - q)
        x = min(1.0, max(0.0, (d - lo) / (hi - lo)))
        if smooth:
            x = x * x * (3.0 - 2.0 * x)
        out.append(1.0 - x)
    return out


def is_off(values) -> bool:
    values = clean(values)
    return values["scanline_opacity"] <= 0 and values["noise_opacity"] <= 0


def load() -> dict:
    """Current settings, read fresh from main_config.json."""
    try:
        with open(paths.confs("main_config.json")) as f:
            config = json.load(f)
        return clean(config.get(CONFIG_KEY) if isinstance(config, dict) else None)
    except FileNotFoundError:
        return dict(DEFAULTS)
    except Exception as e:
        _l.warning("Could not read video_effects from main_config.json: %s", e)
        return dict(DEFAULTS)


def save(values) -> dict:
    """Write the settings to main_config.json; returns the cleaned values."""
    values = clean(values)
    path = paths.confs("main_config.json")
    try:
        with open(path) as f:
            config = json.load(f)
        if not isinstance(config, dict):
            config = {}
    except FileNotFoundError:
        config = {}
    config[CONFIG_KEY] = values
    path.parent.mkdir(parents=True, exist_ok=True)
    from fs42.platform_compat import atomic_write_text

    atomic_write_text(path, json.dumps(config, indent=4))
    return values


# ------------------------------------------------------------------ mpv

def shader_source(values) -> str:
    """A mpv user shader with the settings baked in."""
    v = clean(values)
    pitch, centre, lo, hi, smooth = _edges(v)
    pattern = v["scanline_pattern"]
    if pattern == "horizontal":
        dark = "fs42_line(px.y)"
    elif pattern == "vertical":
        dark = "fs42_line(px.x)"
    else:
        dark = "max(fs42_line(px.x), fs42_line(px.y))"
    return f"""//!HOOK OUTPUT
//!BIND HOOKED
//!DESC FieldStation42 CRT ({pattern} {v['scanline_style']} lines {v['scanline_opacity']:.2f}, {v['scanline_thickness']}/{v['scanline_size']}px; noise {v['noise_opacity']:.2f}/{v['noise_grain']}px)

// Integer hash (lowbias32).  The usual fract(sin(dot())) trick falls apart
// on real GPUs once its argument gets large - after a few minutes the
// "noise" turns into diagonal stripes - so the grain is hashed in integers.
float fs42_hash(uvec2 cell, uint seed) {{
    uint h = cell.x * 1664525u + cell.y * 1013904223u + seed * 2654435761u;
    h ^= h >> 16u; h *= 0x7feb352du; h ^= h >> 15u; h *= 0x846ca68bu; h ^= h >> 16u;
    return float(h & 0xffffffu) / 16777216.0;
}}

// Darkness 0..1 of screen row/column p (same maths as line_profile()).
float fs42_line(float p) {{
    float q = mod(p + 0.5 - {centre:.4f}, {pitch:.1f});
    float d = min(q, {pitch:.1f} - q);
    float x = clamp((d - ({lo:.4f})) / ({hi - lo:.4f}), 0.0, 1.0);
    {"x = x * x * (3.0 - 2.0 * x);" if smooth else ""}
    return 1.0 - x;
}}

vec4 hook() {{
    vec4 c = HOOKED_tex(HOOKED_pos);
    // Screen pixel of this fragment: HOOKED at OUTPUT is the scaled picture,
    // so a row of the texture is a row of the display.
    vec2 px = floor(HOOKED_pos * HOOKED_size);
    c.rgb *= 1.0 - {v['scanline_opacity']:.3f} * {dark};
    // Fresh grain every frame, in GRAIN-pixel cells.
    uvec2 cell = uvec2(floor(px / {float(v['noise_grain']):.1f}));
    float n = fs42_hash(cell, uint(frame));
    c.rgb += (n - 0.5) * {v['noise_opacity']:.3f};
    return c;
}}
"""


_serial = 0


def apply_to_mpv(mpv, values) -> bool:
    """Install (or clear) the shader on a running mpv.  Returns True on success."""
    global _serial
    values = clean(values)
    try:
        if is_off(values):
            mpv.command("change-list", "glsl-shaders", "clr", "")
            return True
        # mpv only reloads a shader when the option value changes, so each
        # version gets its own file name; the previous one is removed.
        _serial += 1
        folder = paths.cache()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"fs42_crt_{os.getpid()}_{_serial}.glsl"
        path.write_text(shader_source(values), encoding="utf-8")
        mpv.command("change-list", "glsl-shaders", "set", str(path))
        previous = folder / f"fs42_crt_{os.getpid()}_{_serial - 1}.glsl"
        try:
            previous.unlink()
        except OSError:
            pass
        return True
    except Exception as e:
        _l.warning("Could not apply video effects to mpv: %s", e)
        return False


def cleanup_shader_files():
    try:
        for path in paths.cache().glob(f"fs42_crt_{os.getpid()}_*.glsl"):
            path.unlink()
    except Exception:
        pass
