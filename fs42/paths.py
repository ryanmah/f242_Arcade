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
    if not IS_FROZEN:
        # Running from a checkout: behave exactly like upstream.
        return _repo_root()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / APP_DIRNAME


def data(*parts) -> Path:
    """Writable user data root (configs, catalog, runtime state, media)."""
    global _cached_data_root
    if _cached_data_root is None:
        override = os.environ.get("FS42_HOME")
        _cached_data_root = Path(override).expanduser().resolve() if override else _default_data_root()
    return _cached_data_root.joinpath(*[str(p) for p in parts])


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
