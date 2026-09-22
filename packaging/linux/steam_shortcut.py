#!/usr/bin/env python3
"""Add or remove FieldStation42 as a non-Steam game.

Steam keeps its non-Steam shortcuts in a small binary "VDF" file per user:
``<steam>/userdata/<id>/config/shortcuts.vdf``.  This reads it, adds (or
replaces) one entry, and writes it back.  No dependencies beyond python3,
which SteamOS ships.

    steam_shortcut.py --add    --exe /path/FieldStation42 --icon /path/icon.png
    steam_shortcut.py --remove
    steam_shortcut.py --list

Steam must not be running while the file is edited: it rewrites the file
from memory on exit and would discard the change.  The installer takes care
of stopping and restarting it; this script only refuses when asked to
``--check-running``.
"""

import argparse
import os
import struct
import sys
import zlib
from pathlib import Path

APP_NAME = "FieldStation42"

# Binary VDF markers.
T_MAP, T_STR, T_INT, T_END = 0x00, 0x01, 0x02, 0x08


# ------------------------------------------------------------- binary VDF

def _read_cstring(data: bytes, pos: int):
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8", "replace"), end + 1


def parse(data: bytes):
    """Parse a binary VDF blob into nested dicts (insertion ordered)."""

    def read_map(pos):
        result = {}
        while pos < len(data):
            kind = data[pos]
            pos += 1
            if kind == T_END:
                return result, pos
            key, pos = _read_cstring(data, pos)
            if kind == T_MAP:
                result[key], pos = read_map(pos)
            elif kind == T_STR:
                result[key], pos = _read_cstring(data, pos)
            elif kind == T_INT:
                result[key] = struct.unpack_from("<I", data, pos)[0]
                pos += 4
            else:
                raise ValueError(f"unsupported VDF type 0x{kind:02x} at {pos - 1}")
        return result, pos

    root, _ = read_map(0)
    return root


def dump(obj: dict) -> bytes:
    out = bytearray()

    def write_map(mapping):
        for key, value in mapping.items():
            k = key.encode("utf-8") + b"\x00"
            if isinstance(value, dict):
                out.append(T_MAP)
                out.extend(k)
                write_map(value)
            elif isinstance(value, int):
                out.append(T_INT)
                out.extend(k)
                out.extend(struct.pack("<I", value & 0xFFFFFFFF))
            else:
                out.append(T_STR)
                out.extend(k)
                out.extend(str(value).encode("utf-8") + b"\x00")
        out.append(T_END)

    write_map(obj)
    return bytes(out)


# -------------------------------------------------------------- locating

def steam_roots():
    home = Path.home()
    for candidate in (
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".steam/debian-installation",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ):
        if (candidate / "userdata").is_dir():
            yield candidate


def shortcut_files(create=False):
    """Every user's shortcuts.vdf under every Steam install found."""
    for root in steam_roots():
        for user in sorted((root / "userdata").iterdir()):
            if not user.name.isdigit() or user.name == "0":
                continue
            config = user / "config"
            path = config / "shortcuts.vdf"
            if path.exists() or (create and config.is_dir()):
                yield path


def steam_is_running() -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as handle:
                if handle.read().strip() in ("steam", "steamwebhelper"):
                    return True
        except OSError:
            continue
    return False


# ----------------------------------------------------------------- entries

def app_id(exe: str, name: str) -> int:
    """Steam's own scheme for non-Steam app ids (crc32 with the top bit set)."""
    return (zlib.crc32((exe + name).encode("utf-8")) | 0x80000000) & 0xFFFFFFFF


def make_entry(exe: str, start_dir: str, icon: str, launch_options: str = "") -> dict:
    quoted = f'"{exe}"'
    return {
        "appid": app_id(quoted, APP_NAME),
        "AppName": APP_NAME,
        "Exe": quoted,
        "StartDir": f'"{start_dir}"',
        "icon": icon or "",
        "ShortcutPath": "",
        "LaunchOptions": launch_options,
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "FlatpakAppID": "",
        "tags": {},
    }


def load(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        return {"shortcuts": {}}
    with open(path, "rb") as handle:
        root = parse(handle.read())
    root.setdefault("shortcuts", {})
    return root


def save(path: Path, root: dict):
    backup = path.with_suffix(".vdf.fs42-backup")
    if path.exists():
        backup.write_bytes(path.read_bytes())
    tmp = path.with_suffix(".vdf.tmp")
    tmp.write_bytes(dump(root))
    os.replace(tmp, path)


def add(exe: str, icon: str, launch_options: str) -> int:
    exe = str(Path(exe).resolve())
    start_dir = str(Path(exe).parent)
    entry = make_entry(exe, start_dir, icon, launch_options)
    touched = 0
    for path in shortcut_files(create=True):
        root = load(path)
        shortcuts = root["shortcuts"]
        replaced = False
        for key, existing in shortcuts.items():
            if isinstance(existing, dict) and existing.get("AppName") == APP_NAME:
                shortcuts[key] = entry
                replaced = True
        if not replaced:
            indices = [int(k) for k in shortcuts if k.isdigit()]
            shortcuts[str(max(indices) + 1 if indices else 0)] = entry
        save(path, root)
        print(f"{'updated' if replaced else 'added'} {APP_NAME} in {path}")
        touched += 1
    if not touched:
        print("No Steam user profiles found; is Steam installed and has it been signed in to?", file=sys.stderr)
        return 1
    return 0


def remove() -> int:
    for path in shortcut_files():
        root = load(path)
        shortcuts = root["shortcuts"]
        kept = {}
        for key, existing in shortcuts.items():
            if isinstance(existing, dict) and existing.get("AppName") == APP_NAME:
                continue
            kept[str(len(kept))] = existing
        if len(kept) != len(shortcuts):
            root["shortcuts"] = kept
            save(path, root)
            print(f"removed {APP_NAME} from {path}")
    return 0


def list_entries() -> int:
    for path in shortcut_files():
        print(path)
        for key, entry in load(path)["shortcuts"].items():
            if isinstance(entry, dict):
                print(f"  [{key}] {entry.get('AppName')}  {entry.get('Exe')}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", action="store_true")
    group.add_argument("--remove", action="store_true")
    group.add_argument("--list", action="store_true")
    parser.add_argument("--exe", help="Path to the FieldStation42 executable (with --add).")
    parser.add_argument("--icon", default="", help="Icon image for the library entry.")
    parser.add_argument("--launch-options", default="", help="Extra arguments Steam passes to the executable.")
    parser.add_argument("--check-running", action="store_true", help="Refuse to edit while Steam is running.")
    args = parser.parse_args(argv)

    if args.check_running and not args.list and steam_is_running():
        print("Steam is running; close it first (it overwrites shortcuts.vdf on exit).", file=sys.stderr)
        return 3
    if args.add:
        if not args.exe:
            parser.error("--add needs --exe")
        return add(args.exe, args.icon, args.launch_options)
    if args.remove:
        return remove()
    return list_entries()


if __name__ == "__main__":
    sys.exit(main())
