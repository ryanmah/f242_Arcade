"""Build station configs the menu can write without breaking the player.

``StationManager.write_station_config`` reloads every station after writing
and calls ``exit(-1)`` if any of them fails to load - a missing content
folder, a standard station without all seven day keys, a template that does
not resolve.  The file is already on disk at that point, so the *player*
would then die on its next reload.

So the menu never hands it a config it has not first proven loadable here,
in the disposable menu process, by running the exact same processing path.
"""

import copy
import json
import logging
import os
import re
from pathlib import Path

from fs42 import paths
# station_manager must be imported before station_io: station_io pulls in
# schedule_hint, which imports station_manager, which imports station_io -
# a cycle that only resolves cleanly from this end.
from fs42.station_manager import StationManager  # noqa: F401
from fs42.station_io import StationIO

_l = logging.getLogger("MENU.FORMS")

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".mpg", ".mpeg",
    ".mp3", ".flac", ".ogg", ".wav", ".m4a",
}


class FormError(ValueError):
    """A problem the user can fix, phrased for the screen."""


# ------------------------------------------------------------------ folders

def media_files(folder: Path):
    try:
        return sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS and not p.name.startswith(".")
        )
    except OSError:
        return []


def subfolders(folder: Path):
    try:
        return sorted(
            p for p in folder.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
    except OSError:
        return []


# Subfolders with these names are filler, not shows.  A folder of media with
# a "bumps" or "commercials" sibling gets real breaks between programmes;
# without them, shows play back to back.
BUMP_FOLDERS = ("bumps", "bump", "station_ids", "idents")
COMMERCIAL_FOLDERS = ("commercials", "commercial", "ads", "adverts")


def _find_named(folder: Path, names):
    for sub in subfolders(folder):
        if sub.name.lower() in names and (media_files(sub) or any(media_files(s) for s in subfolders(sub))):
            return sub
    return None


def describe_folder(folder: Path) -> dict:
    """What kind of station this folder naturally makes."""
    folder = Path(folder)
    subs = subfolders(folder)
    reserved = BUMP_FOLDERS + COMMERCIAL_FOLDERS
    tags = [s for s in subs if s.name.lower() not in reserved and (media_files(s) or subfolders(s))]
    loose = media_files(folder)
    if tags:
        kind = "standard"
    elif loose:
        kind = "loop"
    else:
        kind = None
    bumps = _find_named(folder, BUMP_FOLDERS)
    commercials = _find_named(folder, COMMERCIAL_FOLDERS)
    return {
        "folder": folder,
        "kind": kind,
        "tags": [t.name for t in tags],
        "loose_files": len(loose),
        "bump_dir": bumps.name if bumps else None,
        "commercial_dir": commercials.name if commercials else None,
    }


def suggest_name(folder: Path) -> str:
    raw = Path(folder).name.replace("_", " ").replace("-", " ").strip()
    raw = re.sub(r"\s+", " ", raw)
    return raw.title() if raw else "New Channel"


def next_channel_number(stations) -> int:
    used = {int(s["channel_number"]) for s in stations if "channel_number" in s}
    number = 2
    while number in used:
        number += 1
    return number


def portable_path(folder: Path) -> str:
    """Store paths inside the data folder relatively, so a config keeps
    working if the data folder moves; anything else stays absolute."""
    folder = Path(folder).resolve()
    root = paths.data().resolve()
    try:
        return folder.relative_to(root).as_posix()
    except ValueError:
        return str(folder)


# ------------------------------------------------------------------- configs

def build_station_config(folder, name, channel_number, kind, tags=None) -> dict:
    """The full ``{"station_conf": {...}}`` document for a new station.

    standard: every hour of every day plays the chosen tag folders, with no
    filler (``schedule_increment: 0``), which is the smallest shape that both
    loads and schedules without off-air media, commercials or bumps.

    loop: plays everything in the folder, no schedule at all.
    """
    name = str(name).strip()
    if not name:
        raise FormError("The station needs a name.")
    try:
        channel_number = int(channel_number)
    except (TypeError, ValueError):
        raise FormError("Channel number must be a whole number.")
    if channel_number < 0:
        raise FormError("Channel number must be 0 or higher.")

    folder = Path(folder)
    if not folder.is_dir():
        raise FormError(f"Folder not found: {folder}")

    conf = {
        "network_name": name,
        "channel_number": channel_number,
        "content_dir": portable_path(folder),
    }

    if kind == "loop":
        conf["network_type"] = "loop"
    elif kind == "standard":
        tags = [t for t in (tags or []) if t]
        if not tags:
            raise FormError("Pick at least one folder to play.")
        for tag in tags:
            if not (folder / tag).is_dir():
                raise FormError(f"No folder named {tag!r} inside {folder.name}.")
        conf.update({
            "network_type": "standard",
            "day_templates": {
                "daily": {str(hour): {"tags": list(tags)} for hour in range(24)},
            },
        })
        for day in DAYS:
            conf[day] = "daily"

        # Breaks need filler.  With a bumps and/or commercials folder we can
        # schedule on the half hour like real TV; without them, shows run
        # back to back (schedule_increment 0 asks for no filler at all).
        info = describe_folder(folder)
        if info["bump_dir"] or info["commercial_dir"]:
            conf["schedule_increment"] = 30
            conf["commercial_free"] = info["commercial_dir"] is None
            if info["bump_dir"]:
                conf["bump_dir"] = portable_path(folder / info["bump_dir"])
            if info["commercial_dir"]:
                conf["commercial_dir"] = portable_path(folder / info["commercial_dir"])
        else:
            conf["schedule_increment"] = 0
            conf["commercial_free"] = True
    else:
        raise FormError(f"Unknown station type: {kind!r}")

    return {"station_conf": conf}


def verify_loadable(config: dict):
    """Run the config through the same path a reload would, and raise
    ``FormError`` instead of letting it exit the process.

    Returns the processed station dict on success.
    """
    io = StationIO()
    try:
        ok, message = io.validate_station_config(copy.deepcopy(config))
    except Exception as e:
        raise FormError(f"Invalid config: {e}")
    if not ok:
        # validate_station_config returns a list of problems on failure.
        problems = message if isinstance(message, (list, tuple)) else [message]
        raise FormError("; ".join(str(p) for p in problems) or "Invalid config.")

    try:
        processed = io._process_single_config(copy.deepcopy(config), "<menu>")
    except SystemExit as e:
        raise FormError(f"Config would not load (exit {e.code}).")
    except FileNotFoundError as e:
        raise FormError(str(e))
    except Exception as e:
        raise FormError(f"Config would not load: {type(e).__name__}: {e}")

    if processed is None:
        raise FormError("Config is marked inactive by its active_rules.")

    if processed.get("network_type") == "standard":
        # smooth_tags indexes every day; a missing one is a KeyError outside
        # any try in StationManager.
        from fs42.slot_reader import SlotReader

        try:
            SlotReader.smooth_tags(copy.deepcopy(processed))
        except Exception as e:
            raise FormError(f"Schedule would not load: {type(e).__name__}: {e}")
    return processed


# --------------------------------------------------------------- persistence

def _guarded(fn, *args, **kwargs):
    """Call a StationManager write and turn its exit(-1) into an error."""
    try:
        return fn(*args, **kwargs)
    except SystemExit as e:
        raise FormError(f"Station files failed to reload (exit {e.code}). Check confs/.")


def create_station(config: dict):
    """Validate, verify, write.  Returns (message, file_path)."""
    from fs42.station_manager import StationManager

    verify_loadable(config)
    name = config["station_conf"]["network_name"]
    manager = StationManager()
    if manager.station_by_name(name) is not None:
        raise FormError(f"A station named {name!r} already exists.")
    ok, message, file_path = _guarded(manager.write_station_config, name, config, False)
    if not ok:
        raise FormError(message)
    return message, file_path


def update_station(name: str, mutate):
    """Load the raw file for ``name``, apply ``mutate(station_conf)``, verify, write."""
    from fs42.station_manager import StationManager

    io = StationIO()
    ok, raw, error = io.read_raw_station_config(name)
    if not ok:
        raise FormError(error or f"Could not read {name}.")
    updated = copy.deepcopy(raw)
    mutate(updated["station_conf"])
    verify_loadable(updated)
    ok, message, _ = _guarded(StationManager().write_station_config, name, updated, True)
    if not ok:
        raise FormError(message)
    return message


def delete_station(station: dict, log=None):
    """Remove catalog rows, schedule blocks, then the file."""
    from fs42 import build_jobs
    from fs42.station_manager import StationManager

    build_jobs.delete_station_data(station, log)
    ok, message = _guarded(StationManager().delete_station_config, station["network_name"])
    if not ok:
        raise FormError(message)
    return message


def set_picture(name: str, values):
    from fs42 import picture

    def mutate(conf):
        picture.write_to_station(conf, values)

    return update_station(name, mutate)


def set_hidden(name: str, hidden: bool):
    def mutate(conf):
        conf["hidden"] = bool(hidden)

    return update_station(name, mutate)
