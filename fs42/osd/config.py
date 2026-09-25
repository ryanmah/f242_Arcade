"""On-screen display configuration.

These models are lifted unchanged from the GLFW implementation so existing
``osd/osd.json`` files keep working field for field.  Only the renderer behind
them changed.
"""

import json
import logging
from enum import Enum

from pydantic import BaseModel

from fs42 import paths

_l = logging.getLogger("OSD")


class HAlignment(Enum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    CENTER = "CENTER"


class VAlignment(Enum):
    TOP = "TOP"
    BOTTOM = "BOTTOM"
    CENTER = "CENTER"


class StatusDisplayConfig(BaseModel):
    display_time: float = 2.0
    halign: HAlignment = HAlignment.LEFT
    valign: VAlignment = VAlignment.TOP
    format_text: str = "{channel_number} - {network_name}"
    text_color: tuple[int, int, int, int] = (0, 255, 0, 200)
    font_size: int = 40
    expansion_factor: float = 1.0
    # A font family name, or a font file whose base name is the family.  The
    # default is the menu's VCR face, which the player hands to mpv.
    font: str | None = "VCR OSD Mono"
    # Where the banner's horizontal position is measured from:
    #   "4:3"    - the edge of a 4:3 picture centred on the screen, so the
    #              banner sits in the same place over 4:3 and 16:9 video
    #              (x_offset_px from that edge, in 1080p pixels);
    #   "screen" - upstream's behaviour, x_margin of the whole screen.
    anchor: str = "4:3"
    x_offset_px: float = 30.0
    x_margin: float = 0.12
    y_margin: float = 0.1
    delay: float = 0.0


class VolumeDisplayConfig(BaseModel):
    display_time: float = 5.0
    halign: HAlignment = HAlignment.CENTER
    valign: VAlignment = VAlignment.BOTTOM
    color: tuple[int, int, int, int] = (0, 255, 0, 200)
    width: float = 0.4
    height: float = 0.04
    x_margin: float = 0.1
    y_margin: float = 0.375
    border_thickness: float = 2.0
    padding: float = 0.008


class LogoDisplayConfig(BaseModel):
    halign: HAlignment = HAlignment.RIGHT
    valign: VAlignment = VAlignment.TOP
    width: float = 0.112
    height: float = 0.15
    x_margin: float = 0.05
    y_margin: float = 0.05
    logo_mapping: dict[str, str] = {}
    default_logo: str | None = None
    always_show: bool = False
    display_time: float = 5.0
    default_show_logo: bool = True
    default_logo_permanent: bool = False
    default_logo_alpha: float = 1.0


def load_configs(path=None):
    """Read osd.json into a list of (kind, config) pairs.

    ``kind`` is one of "status", "volume" or "logo".  A HybridDisplay entry
    expands to one logo plus one status config built from the same object,
    which is what upstream intended (its implementation appended stale names
    from the previous loop iteration instead).
    """
    config_path = path or paths.osd_conf()
    entries = []

    if not config_path or not config_path.exists():
        entries.append(("status", StatusDisplayConfig()))
    else:
        try:
            with open(config_path, "r") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as e:
            _l.warning("Could not read %s: %s - using defaults", config_path, e)
            raw = []

        for obj in raw:
            obj = dict(obj)
            kind = obj.pop("type", "StatusDisplay")
            try:
                if kind == "StatusDisplay":
                    entries.append(("status", StatusDisplayConfig.model_validate(obj)))
                elif kind == "LogoDisplay":
                    entries.append(("logo", LogoDisplayConfig.model_validate(obj)))
                elif kind == "HybridDisplay":
                    entries.append(("logo", LogoDisplayConfig.model_validate(obj)))
                    entries.append(("status", StatusDisplayConfig.model_validate(obj)))
                elif kind == "VolumeDisplay":
                    entries.append(("volume", VolumeDisplayConfig.model_validate(obj)))
                else:
                    _l.warning("Unrecognized osd object type: %s", kind)
            except Exception as e:
                _l.warning("Skipping invalid %s entry in %s: %s", kind, config_path, e)

        if not entries:
            entries.append(("status", StatusDisplayConfig()))

    # Always provide a volume meter unless one was configured, matching its
    # colour to the first status display so the OSD reads as one thing.
    if not any(kind == "volume" for kind, _ in entries):
        volume = VolumeDisplayConfig()
        for kind, config in entries:
            if kind == "status":
                volume.color = config.text_color
                break
        entries.append(("volume", volume))

    return entries
