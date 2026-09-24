"""Per-channel picture settings: how the video is scaled and zoomed.

Stored in each station's config with upstream's own keys where they exist:

    video_keepaspect   bool    False stretches the picture to the screen
    panscan            0..1    crop the edges to fill the screen (1 = fill)
    video_zoom         0.5..2  extra zoom on top (1 = none); FieldStation42
                               for Windows/SteamOS addition, applied as
                               mpv's log2 ``video-zoom``

The menu shows the first two as one "Scaling" choice:

    FIT       keep the shape, bars where it does not fit   (keepaspect, panscan 0)
    FILL      keep the shape, crop the edges to fill       (keepaspect, panscan 1)
    STRETCH   stretch to the screen                        (no keepaspect)
"""

import logging
import math

_l = logging.getLogger("PICTURE")

MODES = ["fit", "fill", "stretch"]
ZOOM_LIMITS = (0.5, 2.0)
ZOOM_STEP = 0.05


def clean(values) -> dict:
    values = values if isinstance(values, dict) else {}
    mode = str(values.get("mode", "fit")).lower()
    if mode not in MODES:
        mode = "fit"
    try:
        zoom = float(values.get("zoom", 1.0))
    except (TypeError, ValueError):
        zoom = 1.0
    zoom = round(max(ZOOM_LIMITS[0], min(ZOOM_LIMITS[1], zoom)), 2)
    return {"mode": mode, "zoom": zoom}


def from_station(conf) -> dict:
    """The picture settings a station config describes."""
    conf = conf or {}
    if conf.get("video_keepaspect") is False:
        mode = "stretch"
    elif float(conf.get("panscan") or 0.0) > 0.0:
        mode = "fill"
    else:
        mode = "fit"
    return clean({"mode": mode, "zoom": conf.get("video_zoom", 1.0)})


def write_to_station(conf: dict, values) -> dict:
    """Put picture settings into a raw station_conf dict (in place)."""
    values = clean(values)
    mode = values["mode"]
    if mode == "stretch":
        conf["video_keepaspect"] = False
        conf.pop("panscan", None)
    elif mode == "fill":
        conf.pop("video_keepaspect", None)
        # Keep a partial crop someone set by hand; otherwise crop fully.
        if not float(conf.get("panscan") or 0.0) > 0.0:
            conf["panscan"] = 1.0
    else:
        conf.pop("video_keepaspect", None)
        conf.pop("panscan", None)
    if abs(values["zoom"] - 1.0) < 1e-6:
        conf.pop("video_zoom", None)
    else:
        conf["video_zoom"] = values["zoom"]
    return conf


def mpv_properties(values, panscan=None) -> dict:
    """The mpv properties for these settings."""
    values = clean(values)
    mode = values["mode"]
    return {
        "keepaspect": mode != "stretch",
        "panscan": (panscan if panscan else 1.0) if mode == "fill" else 0.0,
        "video_zoom": math.log2(values["zoom"]),
    }


def apply_to_mpv(mpv, values, panscan=None) -> bool:
    try:
        for name, value in mpv_properties(values, panscan).items():
            setattr(mpv, name, value)
        return True
    except Exception as e:
        _l.warning("Could not apply picture settings: %s", e)
        return False


def apply_station(mpv, conf) -> bool:
    """Apply what a station config asks for (used on every file played)."""
    conf = conf or {}
    return apply_to_mpv(mpv, from_station(conf), panscan=conf.get("panscan"))
