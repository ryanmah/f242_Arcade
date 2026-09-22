"""Which logo should be on screen right now.

This is the selection half of the old ``logo_display.py`` with all the OpenGL
removed, so it can drive any renderer.  The hierarchy and the tag/temporal
folder conventions are unchanged, and existing logo directory layouts keep
working.
"""

import datetime
import glob
import logging
import random
from pathlib import Path

from fs42 import paths
from fs42.osd.content_classifier import ContentType, classify_current_content
from fs42.station_manager import StationManager

_l = logging.getLogger("OSD.LOGO")

_IMAGE_GLOBS = ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp")


def available_logos(dir_path: Path):
    found = []
    for extension in _IMAGE_GLOBS:
        found.extend(glob.glob(str(dir_path / extension)))
        found.extend(glob.glob(str(dir_path / extension.upper())))
    return [Path(p) for p in sorted(set(found))]


def current_weekday() -> str:
    return datetime.date.today().strftime("%A")


def current_day_part():
    """Name of the day_part covering the current hour, from main_config."""
    day_parts = StationManager().server_conf.get("day_parts")
    if not isinstance(day_parts, dict):
        return None
    hour = datetime.datetime.now().hour
    for name, spec in day_parts.items():
        # StationManager normalizes these into ranges/lists of hours.
        try:
            if hour in spec:
                return name
        except TypeError:
            continue
    return None


def _normalize_tag(tag: str):
    stripped = str(tag).strip()
    safe = stripped.replace(" ", "_").replace("/", "_").replace("\\", "_").replace(":", "_")
    return stripped, safe


def _logo_dir_for_tag(base_dir: Path, tag: str):
    stripped, safe = _normalize_tag(tag)
    if not stripped:
        return None
    for candidate in (base_dir / safe, base_dir / stripped):
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def find_content_logo_dir(base_dir: Path, station_data: dict, status: dict):
    """Tag folder for the currently playing content.

    Prefers the tag implied by the playing file's own path
    (content_dir/<tag>/file), falling back to the schedule slot's first tag.
    """
    file_path = status.get("file_path")
    content_dir = station_data.get("content_dir")

    if file_path and content_dir:
        file_parts = Path(file_path).parts
        content_parts = Path(content_dir).parts
        for i in range(0, len(file_parts) - len(content_parts) + 1):
            if file_parts[i:i + len(content_parts)] == content_parts:
                tag_index = i + len(content_parts)
                if tag_index < len(file_parts) - 1:
                    match = _logo_dir_for_tag(base_dir, file_parts[tag_index])
                    if match:
                        return match
                break
    elif file_path:
        match = _logo_dir_for_tag(base_dir, Path(file_path).parent.name)
        if match:
            return match

    now = datetime.datetime.now()
    day_schedule = station_data.get(now.strftime("%A").lower())
    if not isinstance(day_schedule, dict):
        return None

    slot = day_schedule.get(str(now.hour))
    if slot is None:
        hours = sorted(int(h) for h in day_schedule.keys() if h.isdigit())
        eligible = [h for h in hours if h <= now.hour]
        if eligible:
            slot = day_schedule.get(str(eligible[-1]))

    if not isinstance(slot, dict):
        return None

    tags = slot.get("tags") or slot.get("tag")
    if isinstance(tags, str):
        first = tags
    elif isinstance(tags, (list, tuple)) and tags:
        first = str(tags[0])
    else:
        return None
    return _logo_dir_for_tag(base_dir, first)


def find_temporal_logo_dir(base_dir: Path):
    """A month folder, either "October" or a "October 1 - October 31" range."""
    today = datetime.date.today()
    month_name = today.strftime("%B")

    if not base_dir.exists():
        return None

    for sub in base_dir.iterdir():
        if not sub.is_dir():
            continue
        if sub.name == month_name:
            return sub
        parts = sub.name.split(" - ")
        if len(parts) != 2:
            continue
        left, right = parts[0].split(), parts[1].split()
        if len(left) == 2 and len(right) == 2 and left[0] == right[0] == month_name:
            try:
                if int(left[1]) <= today.day <= int(right[1]):
                    return sub
            except ValueError:
                continue
    return None


def select_logo(station_data: dict, status: dict):
    """Pick a logo file for a station.

    Hierarchy, most specific first:
      1. tag folder            logo_dir/<tag>/
      2. month/weekday/daypart logo_dir/<Month>/<Weekday>/<day_part>/
      3. month/weekday         logo_dir/<Month>/<Weekday>/
      4. month/daypart         logo_dir/<Month>/<day_part>/
      5. month                 logo_dir/<Month>/
      6. weekday/daypart       logo_dir/<Weekday>/<day_part>/
      7. weekday               logo_dir/<Weekday>/
      8. daypart               logo_dir/<day_part>/
      9. logo_dir itself
    """
    multi = str(station_data.get("multi_logo", "single")).lower()
    multi = {"off": "single", "random": "multi"}.get(multi, multi)
    if multi not in ("single", "multi"):
        multi = "single"

    content_dir = station_data.get("content_dir")
    logo_dir = station_data.get("logo_dir")
    default_logo_name = station_data.get("default_logo")

    # Upstream semantics: logo_dir is relative to content_dir.  Both have
    # already been made absolute by StationIO.
    if content_dir and logo_dir:
        logo_path = Path(logo_dir)
        base_dir = logo_path if logo_path.is_absolute() else Path(content_dir) / logo_dir
        if not base_dir.exists() and logo_path.is_absolute():
            base_dir = Path(content_dir) / logo_path.name
    elif logo_dir:
        base_dir = paths.resolve_user_path(logo_dir)
    else:
        return None

    candidates = []

    def add(path):
        if path and path.exists() and path.is_dir() and path not in candidates:
            candidates.append(path)

    add(find_content_logo_dir(base_dir, station_data, status))

    month_dir = find_temporal_logo_dir(base_dir)
    day_part = current_day_part()
    day_part_folder = day_part.replace(" ", "_") if day_part else None
    weekday = current_weekday()

    if month_dir:
        if day_part_folder:
            add(month_dir / weekday / day_part_folder)
        add(month_dir / weekday)
        if day_part_folder:
            add(month_dir / day_part_folder)
        add(month_dir)
    else:
        if day_part_folder:
            add(base_dir / weekday / day_part_folder)
        add(base_dir / weekday)
        if day_part_folder:
            add(base_dir / day_part_folder)

    if multi == "single":
        for directory in candidates:
            logos = available_logos(directory)
            if logos:
                if default_logo_name:
                    named = directory / default_logo_name
                    if named.exists():
                        return named
                return random.choice(logos)
        if default_logo_name:
            named = base_dir / default_logo_name
            if named.exists():
                return named
        logos = available_logos(base_dir)
        return random.choice(logos) if logos else None

    pool = []
    for directory in candidates:
        pool.extend(available_logos(directory))
    if not pool:
        pool = available_logos(base_dir)
    return random.choice(pool) if pool else None


class LogoState:
    """Tracks which logo should be showing and for how long."""

    def __init__(self, config):
        self.config = config
        self.station_manager = StationManager()
        self.channel_config = {}
        self.current_info = {}
        self.current_content_type = ContentType.UNKNOWN
        self.current_logo_path = None
        self.time_since_change = float("inf")
        self.using_default_logo = False

    def update(self, dt, status):
        self.time_since_change += dt
        if not status:
            return

        previous_network = self.current_info.get("network_name")
        previous_title = self.current_info.get("title")
        previous_type = self.current_content_type

        self.current_info = status
        self.current_content_type = classify_current_content(status=status)

        network = status.get("network_name")
        title = status.get("title")
        multi = str(self.channel_config.get("multi_logo", "single")).lower()
        if multi == "random":
            multi = "multi"

        if network != previous_network:
            self.time_since_change = 0.0
            self._reload(status)
        elif title != previous_title:
            if previous_type != ContentType.FEATURE and self.current_content_type == ContentType.FEATURE:
                self.time_since_change = 0.0
            self._reload(status)
        elif previous_type != ContentType.FEATURE and self.current_content_type == ContentType.FEATURE:
            self.time_since_change = 0.0
            if multi == "multi":
                self._reload(status)

    def _reload(self, status):
        self.channel_config = {}
        self.using_default_logo = False
        network = status.get("network_name")
        selected = None

        if network:
            try:
                station = self.station_manager.station_by_name(network)
            except Exception as e:
                _l.debug("Could not read station data for %s: %s", network, e)
                station = None
            if isinstance(station, dict):
                self.channel_config = station
                if station.get("show_logo", True) is False:
                    self.current_logo_path = None
                    return
                selected = select_logo(station, status)

        if selected is None and self.config.default_show_logo and self.config.default_logo:
            candidate = paths.resolve_user_path(self.config.default_logo)
            if candidate and candidate.exists():
                selected = candidate
                self.using_default_logo = True

        self.current_logo_path = str(selected) if selected and selected.exists() else None

    @property
    def visible(self) -> bool:
        if not self.current_logo_path:
            return False
        if self.current_content_type != ContentType.FEATURE:
            return False
        if self.config.always_show:
            return True
        if self.using_default_logo:
            if self.config.default_logo_permanent:
                return True
            display_time = self.config.display_time
        else:
            if self.channel_config.get("logo_permanent", False):
                return True
            configured = self.channel_config.get("logo_display_time")
            display_time = float(configured) if configured is not None else self.config.display_time
        return self.time_since_change < display_time

    @property
    def alpha(self) -> float:
        if self.using_default_logo:
            return float(self.config.default_logo_alpha)
        return float(self.channel_config.get("logo_alpha", self.config.default_logo_alpha))

    def geometry(self):
        """Returns (width, height, x_margin, y_margin, halign, valign) as fractions."""
        from fs42.osd.config import HAlignment, VAlignment

        config = self.config
        width = float(self.channel_config.get("logo_width", config.width))
        height = float(self.channel_config.get("logo_height", config.height))
        x_margin = float(self.channel_config.get("logo_x_margin", config.x_margin))
        y_margin = float(self.channel_config.get("logo_y_margin", config.y_margin))

        try:
            halign = HAlignment[str(self.channel_config.get("logo_halign", config.halign.value)).upper()]
        except KeyError:
            halign = config.halign
        try:
            valign = VAlignment[str(self.channel_config.get("logo_valign", config.valign.value)).upper()]
        except KeyError:
            valign = config.valign

        return width, height, x_margin, y_margin, halign, valign
