"""Clean up the environment FieldStation42 is launched with.

Steam starts a non-Steam game with two things a self-contained app does not
want:

* ``LD_LIBRARY_PATH`` pointing into the Steam Runtime (``ubuntu12_32``,
  ``steam-runtime``, ``pinned_libs_*``): libraries from 2012-era Ubuntu
  that shadow the system's own libX11, libxcb, libGL and friends.  Qt, Tk
  and mpv load those, fail, and the player never gets a window up - in Game
  Mode that is the launch spinner that never finishes.
* ``LD_PRELOAD`` with Steam's in-game overlay (``gameoverlayrenderer.so``),
  which hooks every process's graphics calls.  Game Mode draws its own
  Steam menu, so nothing is lost without it.

And in Game Mode everything runs under gamescope, which only hosts X11
clients: Qt is pinned to xcb there so it never tries the compositor's
private Wayland socket.

``prepare()`` runs first thing in every top-level launch.  For the packaged
app it re-executes itself once with the cleaned environment, because the
bootloader has already put the Steam paths into this process's library
search path by the time Python runs.
"""

import os
import sys

_MARK = "FS42_LAUNCH_ENV_CLEAN"
_STEAM_LIB_MARKERS = ("steam-runtime", "ubuntu12_32", "ubuntu12_64", "pinned_libs", "SteamLinuxRuntime")
_OVERLAY_MARKERS = ("gameoverlayrenderer",)


def under_gamescope(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("GAMESCOPE_WAYLAND_DISPLAY")) or env.get("XDG_CURRENT_DESKTOP", "").lower() == "gamescope"


def launched_by_steam(env=None) -> bool:
    env = os.environ if env is None else env
    return any(env.get(k) for k in ("SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID", "SteamClientLaunch"))


def _split(value: str, sep: str):
    return [p for p in (value or "").split(sep) if p]


def clean_library_path(value: str):
    """(cleaned value or None, removed entries)"""
    kept, removed = [], []
    for part in _split(value, ":"):
        (removed if any(m in part for m in _STEAM_LIB_MARKERS) else kept).append(part)
    return (":".join(kept) or None), removed


def clean_preload(value: str):
    parts = [p for chunk in (value or "").split() for p in chunk.split(":") if p]
    kept = [p for p in parts if not any(m in p for m in _OVERLAY_MARKERS)]
    return (" ".join(kept) or None), [p for p in parts if p not in kept]


def cleaned(env) -> tuple:
    """(new environment, list of human-readable changes)."""
    env = dict(env)
    changes = []
    frozen = getattr(sys, "frozen", False)
    # In the packaged app the bootloader has already prefixed the bundle and
    # kept the caller's value in LD_LIBRARY_PATH_ORIG; the caller's value is
    # what matters, and the bootloader adds the bundle again on re-exec.
    original_key = "LD_LIBRARY_PATH_ORIG" if frozen and "LD_LIBRARY_PATH_ORIG" in env else "LD_LIBRARY_PATH"
    original = env.get(original_key, "")
    value, removed = clean_library_path(original)
    if removed:
        changes.append(f"dropped {len(removed)} Steam Runtime library folder(s) from LD_LIBRARY_PATH")
        if frozen:
            env.pop("LD_LIBRARY_PATH_ORIG", None)
        if value:
            env["LD_LIBRARY_PATH"] = value
        else:
            env.pop("LD_LIBRARY_PATH", None)
    preload, dropped = clean_preload(env.get("LD_PRELOAD", ""))
    if dropped:
        changes.append("dropped the Steam overlay from LD_PRELOAD")
        if preload:
            env["LD_PRELOAD"] = preload
        else:
            env.pop("LD_PRELOAD", None)
    if under_gamescope(env):
        if env.get("QT_QPA_PLATFORM") is None:
            env["QT_QPA_PLATFORM"] = "xcb"
            changes.append("Qt pinned to X11 under gamescope")
        if env.pop("WAYLAND_DISPLAY", None) is not None:
            changes.append("WAYLAND_DISPLAY unset under gamescope")
    return env, changes


def summary(env=None) -> str:
    env = os.environ if env is None else env
    keys = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "GAMESCOPE_WAYLAND_DISPLAY",
            "QT_QPA_PLATFORM", "SteamAppId", "SteamGameId", "SteamDeck", "SteamGamepadUI")
    parts = [f"{k}={env[k]}" for k in keys if env.get(k)]
    parts.append(f"LD_LIBRARY_PATH={env.get('LD_LIBRARY_PATH', '')!r}")
    parts.append(f"LD_PRELOAD={env.get('LD_PRELOAD', '')!r}")
    return " ".join(parts)


_changes = []


def prepare():
    """Clean the environment for this launch (and re-exec if we must)."""
    if not sys.platform.startswith("linux"):
        return
    if os.environ.get(_MARK):
        _changes.extend(c for c in os.environ.get(_MARK, "").split(";") if c and c != "1")
        return
    env, changes = cleaned(os.environ)
    if not changes:
        return
    _changes.extend(changes)
    env[_MARK] = ";".join(changes) or "1"
    needs_reexec = any("LD_" in c for c in changes)
    if needs_reexec and getattr(sys, "frozen", False):
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            os.execve(sys.executable, [sys.executable] + sys.argv[1:], env)
        except OSError:
            pass
    os.environ.clear()
    os.environ.update(env)


def changes():
    return list(_changes)
