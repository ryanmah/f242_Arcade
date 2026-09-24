"""Filesystem layout resolution for FieldStation42.

Upstream FieldStation42 resolves every path relative to the current working
directory and assumes it is launched from a git checkout.  That does not work
for a frozen single-file executable, where the code lives in a read-only
extraction directory that is deleted on exit.

This module separates three roots that upstream conflated:

``resources()``
    Read-only assets that ship inside the executable (web console static
    files, JSON schema, example configs, default media).  When frozen this is
    ``sys._MEIPASS``; from source it is the repo root.

``data()``
    Everything the user owns and we must never clobber: station configs, the
    catalog database, schedules, media.  When frozen this is a per-user
    application data directory; from source it is the repo root, so running
    from a checkout behaves exactly as it always has.

``cache()``
    Regenerable scratch (TMDB responses, rendered OSD bitmaps, mpv IPC
    endpoints).  Safe to delete at any time.

Set ``FS42_HOME`` to override ``data()``.  This is what the test-suite and the
``--portable`` flag use.
"""

import logging
import json
import os
import shutil
import sys
from pathlib import Path

_l = logging.getLogger("PATHS")

IS_FROZEN = bool(getattr(sys, "frozen", False))

APP_NAME = "FieldStation42"
APP_DIRNAME = "fieldstation42"

# Files seeded into data()/runtime/ on first run.  Sourced from docs/ in the
# repo, which the PyInstaller spec bundles as resources.
_SEED_MEDIA = (
    "static.mp4",
    "standby.png",
    "brb.png",
    "off_air_pattern.mp4",
    "signoff.mp4",
)

_cached_data_root = None


def _repo_root() -> Path:
    """The checkout root, i.e. the directory containing fs42/."""
    return Path(__file__).resolve().parents[1]


def resources(*parts) -> Path:
    """Read-only bundled assets."""
    if IS_FROZEN:
        base = Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    else:
        base = _repo_root()
    return base.joinpath(*[str(p) for p in parts])


def _default_data_root() -> Path:
    # Where the launcher settings live and where data goes unless redirected.
    # FS42_DEFAULT_HOME exists so tests can exercise the redirect logic
    # without touching a real profile; FS42_HOME (see data()) is the user
    # facing override and wins over everything.
    forced_default = os.environ.get("FS42_DEFAULT_HOME")
    if forced_default:
        return Path(forced_default).expanduser().resolve()
    if not IS_FROZEN:
        # Running from a checkout: behave exactly like upstream.
        return _repo_root()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / APP_DIRNAME


# --------------------------------------------------------------------------
# Launcher settings: a tiny file in the *default* location that can point the
# data root somewhere else (an external drive with an existing confs/ and
# catalog/, say).  It is read once per process, so a change takes effect on
# the next start - the supervisor offers a restart for exactly that.
# --------------------------------------------------------------------------

LAUNCHER_FILE = "launcher.json"


def launcher_settings_path() -> Path:
    return _default_data_root() / LAUNCHER_FILE


def read_launcher_settings() -> dict:
    try:
        with open(launcher_settings_path()) as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def write_launcher_settings(settings: dict):
    target = launcher_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    with open(tmp, "w") as handle:
        json.dump(settings, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, target)


def configured_data_root():
    """The data root chosen in the launcher settings, or None."""
    value = read_launcher_settings().get("data_root")
    if not value:
        return None
    try:
        return Path(str(value)).expanduser()
    except (TypeError, ValueError):
        return None


def set_data_root(path):
    """Point the data root at ``path`` (None restores the default).

    Only the launcher file changes; running processes keep their current
    root until restarted.
    """
    settings = read_launcher_settings()
    if path:
        settings["data_root"] = str(Path(str(path)).expanduser())
    else:
        settings.pop("data_root", None)
    write_launcher_settings(settings)


def _resolve_data_root():
    override = os.environ.get("FS42_HOME")
    if override:
        return Path(override).expanduser().resolve(), "environment"
    configured = configured_data_root()
    if configured is not None and configured.is_dir():
        return configured.resolve(), "settings"
    return _default_data_root(), "default"


def data(*parts) -> Path:
    """Writable user data root (configs, catalog, runtime state, media)."""
    global _cached_data_root
    if _cached_data_root is None:
        _cached_data_root = _resolve_data_root()[0]
    return _cached_data_root.joinpath(*[str(p) for p in parts])


def inspect_data_root(path) -> dict:
    """What a candidate data folder looks like, for the settings page."""
    candidate = Path(str(path)).expanduser()
    info = {
        "path": str(candidate),
        "exists": candidate.exists(),
        "is_dir": candidate.is_dir(),
        "writable": False,
        "station_configs": 0,
        "has_catalog": False,
        "has_schedules": False,
        "empty": False,
        "looks_like_fs42": False,
        "problems": [],
    }
    if not candidate.exists():
        info["problems"].append("That folder does not exist. Is the drive connected?")
        return info
    if not candidate.is_dir():
        info["problems"].append("That path is a file, not a folder.")
        return info
    try:
        probe = candidate / ".fs42-write-test"
        probe.write_text("ok")
        probe.unlink()
        info["writable"] = True
    except OSError:
        info["problems"].append("FieldStation42 cannot write there (read-only drive or permissions).")
    try:
        entries = list(candidate.iterdir())
    except OSError:
        entries = []
    info["empty"] = not entries
    confs_dir = candidate / "confs"
    if confs_dir.is_dir():
        info["station_configs"] = len([
            f for f in confs_dir.glob("*.json") if f.name != "main_config.json"
        ])
    info["has_catalog"] = (candidate / "catalog").is_dir()
    info["has_schedules"] = (candidate / "runtime").is_dir()
    info["looks_like_fs42"] = confs_dir.is_dir() or info["has_catalog"]
    if entries and not info["looks_like_fs42"]:
        info["problems"].append(
            "This folder has files in it but no confs/ or catalog/ folder; FieldStation42 would "
            "create its own folders alongside them."
        )
    return info


def data_root_info() -> dict:
    """Current, configured and default data roots, for the settings page."""
    current = data()
    resolved, source = _resolve_data_root()
    configured = configured_data_root()
    return {
        "current": str(current),
        "default": str(_default_data_root()),
        "configured": str(configured) if configured else None,
        "configured_exists": bool(configured and configured.is_dir()),
        "source": source,
        "locked_by_environment": bool(os.environ.get("FS42_HOME")),
        "restart_required": resolved != current,
        "settings_file": str(launcher_settings_path()),
    }


def cache(*parts) -> Path:
    """Regenerable scratch space."""
    if not IS_FROZEN and not os.environ.get("FS42_HOME"):
        base = _repo_root() / "runtime" / "cache"
    elif os.name == "nt":
        base = data("cache")
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = (Path(xdg) / APP_DIRNAME) if xdg and IS_FROZEN else data("cache")
    base.mkdir(parents=True, exist_ok=True)
    return base.joinpath(*[str(p) for p in parts])


def reset_cached_roots():
    """Forget the memoized data root.  Only used by tests."""
    global _cached_data_root
    _cached_data_root = None


# --------------------------------------------------------------------------
# Named locations
# --------------------------------------------------------------------------

def confs(*parts) -> Path:
    return data("confs", *parts)


def runtime(*parts) -> Path:
    return data("runtime", *parts)


def catalog_root() -> Path:
    return data("catalog")


def logs(*parts) -> Path:
    return data("logs", *parts)


def osd_conf() -> Path:
    return data("osd", "osd.json")


def static_dir() -> Path:
    """Bundled web console assets."""
    return resources("fs42", "fs42_server", "static")


def static_overlay(*parts) -> Path:
    """User-supplied web assets that shadow the bundled ones by name.

    The web console lets users drop in custom themes, bump videos and PPV art.
    Those land here rather than inside the read-only bundle.
    """
    return data("static", *parts)


def schema_path() -> Path:
    return resources("fs42", "station_config_schema.json")


def plugin_dir(name) -> Path:
    return data("plugins", name)


# --------------------------------------------------------------------------
# Bundled executables
# --------------------------------------------------------------------------

def platform_tag() -> str:
    import platform as _platform

    machine = _platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        arch = machine
    system = "windows" if os.name == "nt" else ("macos" if sys.platform == "darwin" else "linux")
    return f"{system}-{arch}"


def bin_dir() -> Path:
    return resources("bin", platform_tag())


def menu_fonts_dir() -> Path:
    """The bundled VCR font, shared by the menu and mpv's on-screen display."""
    return resources("fs42", "menu", "fonts")


def bundled_manifest() -> dict:
    """What fetch_binaries.py bundled: ``{name: {url, sha256, version, ...}}``.

    Empty when running from source or when the build shipped no binaries.
    """
    manifest = bin_dir() / "MANIFEST.json"
    try:
        with open(manifest) as handle:
            return json.load(handle).get("binaries", {})
    except (OSError, ValueError):
        return {}


def bin_path(name: str):
    """Locate a helper executable (``mpv``, ``ffprobe``, ``ffmpeg``).

    Order: explicit config override, bundled copy, then whatever is on PATH.
    Returns ``None`` when nothing is found so callers can emit a useful error.
    """
    exe = f"{name}.exe" if os.name == "nt" else name

    # 1. user override in main_config.json (avoid importing StationManager at
    #    module scope - this module is imported by StationManager itself).
    try:
        from fs42.station_manager import StationManager

        override = StationManager().server_conf.get(f"{name}_path")
        if override:
            candidate = Path(override).expanduser()
            if candidate.exists():
                return candidate
            _l.warning("Configured %s_path does not exist: %s", name, candidate)
    except Exception:
        pass

    # 2. bundled
    candidate = bin_dir() / exe
    if candidate.exists():
        return candidate

    # 3. system
    found = shutil.which(name)
    return Path(found) if found else None


# --------------------------------------------------------------------------
# User-supplied paths from station configs
# --------------------------------------------------------------------------

def resolve_user_path(value) -> Path:
    """Normalize a path that came out of a station config.

    Existing configs contain CWD-relative paths like ``catalog/nbc_catalog``
    and ``runtime/signoff.mp4``.  Those keep working verbatim; they simply
    resolve against ``data()`` instead of the process working directory.
    Absolute paths and ``~`` are honoured as written.
    """
    if value is None:
        return None
    raw = str(value)
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return data(raw)


_relocated = {}


def locate_media(value):
    """Find a media file named in a catalog or schedule, wherever it is now.

    Catalogs remember absolute paths from the machine that built them.  A
    data folder carried over from a Pi (``/home/pi/FieldStation42/catalog/
    NickTV/a.mp4``) or built with relative paths (``catalog/NickTV/a.mp4``)
    still has the files - under this data folder.  Try, in order: the path
    as given, the path relative to the data folder, then every suffix of a
    foreign absolute path under the data folder (``catalog/NickTV/a.mp4``,
    ``NickTV/a.mp4`` ...).  Returns the original string when nothing matches
    so the caller's own error reporting still names it.
    """
    if not value or not isinstance(value, (str, os.PathLike)):
        return value
    raw = str(value)
    if os.path.exists(raw):
        return raw
    cached = _relocated.get(raw)
    if cached is not None and os.path.exists(cached):
        return cached
    resolved = resolve_user_path(raw)
    if resolved is not None and resolved.exists():
        _relocated[raw] = str(resolved)
        return str(resolved)
    parts = [p for p in raw.replace("\\", "/").split("/") if p and not p.endswith(":")]
    root = data()
    # Need at least a folder and a file name to call it a match.
    for start in range(1, len(parts) - 1):
        candidate = root.joinpath(*parts[start:])
        if candidate.exists():
            _relocated[raw] = str(candidate)
            return str(candidate)
    return raw


def sandbox_roots():
    """Directories the media API is permitted to read from."""
    roots = {data(), runtime(), catalog_root(), static_dir(), static_overlay()}
    try:
        from fs42.station_manager import StationManager

        for station in StationManager().stations:
            for key in ("content_dir", "logo_dir", "commercial_dir", "bump_dir"):
                value = station.get(key)
                if value:
                    resolved = resolve_user_path(value)
                    if resolved:
                        roots.add(resolved)
    except Exception:
        pass
    out = []
    for root in roots:
        try:
            out.append(Path(os.path.realpath(str(root))))
        except OSError:
            continue
    return out


# --------------------------------------------------------------------------
# First-run seeding
# --------------------------------------------------------------------------

_DATA_DIRS = (
    "confs",
    "confs/examples",
    "runtime",
    "runtime/guide_videos",
    "runtime/logo_images",
    "catalog",
    "osd",
    "osd/examples",
    "logs",
    "static",
    "plugins",
)


def app_version() -> str:
    try:
        from fs42 import __version__

        return __version__
    except Exception:
        return "0.0.0"


def first_run_seed(force_examples=False) -> bool:
    """Create the user data tree and populate it with defaults.

    Idempotent and non-destructive: user files are never overwritten.  Example
    configs and OSD presets are documentation rather than user data, so they
    are refreshed when the app version changes.

    Returns True if this looked like a first run.
    """
    root = data()
    stamp = root / ".fs42_version"
    fresh = not stamp.exists()

    for relative in _DATA_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)

    previous = stamp.read_text().strip() if stamp.exists() else ""
    refresh = force_examples or previous != app_version()

    # Documentation-grade assets: refresh on upgrade.
    _copy_tree(resources("confs", "examples"), confs("examples"), overwrite=refresh)
    _copy_tree(resources("osd", "examples"), data("osd", "examples"), overwrite=refresh)

    # User-owned: never overwrite.
    _copy_file(resources("osd", "osd.json"), osd_conf(), overwrite=False)

    main_config = confs("main_config.json")
    if not main_config.exists():
        main_config.write_text("{\n}\n")

    # Default media (station reference art, sign-off and static footage).
    for name in _SEED_MEDIA:
        _copy_file(resources("docs", name), runtime(name), overwrite=False)
    _copy_tree(resources("docs", "logo_images"), runtime("logo_images"), overwrite=False)

    if fresh:
        _l.info("Initialized FieldStation42 data directory at %s", root)
    stamp.write_text(app_version())
    return fresh


def _copy_file(source: Path, target: Path, overwrite: bool) -> bool:
    try:
        if not source.exists():
            return False
        if target.exists() and not overwrite:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(target))
        return True
    except OSError as e:
        _l.warning("Could not seed %s: %s", target, e)
        return False


def _copy_tree(source: Path, target: Path, overwrite: bool) -> int:
    if not source.exists() or not source.is_dir():
        return 0
    count = 0
    target.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        destination = target / entry.name
        if entry.is_dir():
            count += _copy_tree(entry, destination, overwrite)
        elif _copy_file(entry, destination, overwrite):
            count += 1
    return count


def describe() -> dict:
    """Layout summary, surfaced by the diagnostics endpoint and --version."""
    return {
        "frozen": IS_FROZEN,
        "version": app_version(),
        "resources": str(resources()),
        "data": str(data()),
        "cache": str(cache()),
        "bin": str(bin_dir()),
        "platform_tag": platform_tag(),
    }
