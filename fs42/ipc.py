"""Cross-process state bus.

Upstream coordinates the player, the API server and the OSD through plain
files in ``runtime/`` (``channel.socket``, ``play_status.socket``,
``volume.socket``) plus a ``shelve`` for the last channel.  They are named
"socket" but they are ordinary files, read and truncated without locking.

That is racy on Linux and outright broken on Windows:

* a writer's ``open(path, "w")`` raises PermissionError while a reader holds
  the file open;
* read-then-truncate loses commands that arrive between the two calls;
* ``shelve`` picks a dbm backend by availability - ``dbm.gnu`` on Linux,
  ``dbm.dumb`` on Windows - producing incompatible files, and ``dbm.dumb`` is
  not safe for concurrent access at all.

This module replaces all of it with a single SQLite database in WAL mode.
SQLite is stdlib, is already a dependency (the catalog lives in one), gives
real cross-process locking on both platforms, needs no port and therefore no
firewall prompt, and is inspectable with any sqlite tool when a user reports a
bug.

For compatibility with existing user scripts that tail
``runtime/play_status.socket``, status writes are mirrored to the legacy files
(atomically) when ``legacy_socket_files`` is enabled.
"""

import json
import logging
import os
import sqlite3
import threading
import time

from fs42 import paths
from fs42 import platform_compat

_l = logging.getLogger("IPC")

DB_NAME = "fs42_state.db"

KEY_STATUS = "player_status"
KEY_VOLUME = "volume"
KEY_CHANNEL_INDEX = "channel_index"
KEY_SHUTDOWN = "shutdown_requested"

TOPIC_CHANNEL = "channel"
TOPIC_PLAYER_CMD = "player_cmd"
# Navigation events for the in-app menu (from mpv key bindings, the phone
# remote, or a gamepad).  Payload: {"action": "<fs42.menu.input.Action>"}.
TOPIC_MENU_INPUT = "menu_input"

# Set by the player while its menu process is alive, so key bindings know
# whether to forward keys or ignore them.
KEY_MENU_OPEN = "menu_open"
# What the menu is showing right now: {"title", "subtitle", "cursor", "rows": [...]}.
# Lets the phone remote mirror the on-screen menu, and lets tests see it.
KEY_MENU_PAGE = "menu_page"

_local = threading.local()
_db_path_override = None
_legacy_enabled = None


def db_path() -> str:
    if _db_path_override:
        return _db_path_override
    return str(paths.runtime(DB_NAME))


def set_db_path(path):
    """Point the bus at an explicit file.  Used by tests."""
    global _db_path_override
    _db_path_override = str(path) if path else None
    close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    claimed_by  TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_pending
    ON events(topic, id) WHERE claimed_by IS NULL;
"""


def _ensure_schema(connection):
    try:
        connection.executescript(_SCHEMA)
    except sqlite3.Error as e:
        _l.warning("Could not create state schema: %s", e)


def _connect() -> sqlite3.Connection:
    connection = getattr(_local, "connection", None)
    if connection is not None and getattr(_local, "path", None) == db_path():
        return connection
    if connection is not None:
        try:
            connection.close()
        except sqlite3.Error:
            pass

    target = db_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    connection = sqlite3.connect(target, timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=2000")
    # Any process may be the first to open the bus (the API server can start
    # before the player), so every connection ensures the schema rather than
    # relying on a particular process having run init_db first.
    _ensure_schema(connection)
    _local.connection = connection
    _local.path = target
    return connection


def close():
    connection = getattr(_local, "connection", None)
    if connection is not None:
        try:
            connection.close()
        except sqlite3.Error:
            pass
    _local.connection = None
    _local.path = None


def init_db():
    """Create the schema and clear stale state from a previous run.

    Called by the supervisor before any child starts.  The schema itself is
    created on every connection, so this is about the cleanup.
    """
    connection = _connect()
    try:
        connection.execute("DELETE FROM events WHERE claimed_by IS NOT NULL")
        connection.execute("DELETE FROM kv WHERE key = ?", (KEY_SHUTDOWN,))
    except sqlite3.Error as e:
        _l.warning("Could not reset state: %s", e)
    return connection


# --------------------------------------------------------------------------
# Key/value state
# --------------------------------------------------------------------------

def set_state(key: str, value):
    payload = json.dumps(value)
    try:
        _connect().execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, payload, time.time()),
        )
    except sqlite3.Error as e:
        _l.warning("Could not write state %s: %s", key, e)


def get_state(key: str, default=None):
    try:
        row = _connect().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error as e:
        _l.warning("Could not read state %s: %s", key, e)
        return default
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return default


def get_state_with_time(key: str):
    """Returns ``(value, updated_at)`` or ``(None, 0.0)``."""
    try:
        row = _connect().execute(
            "SELECT value, updated_at FROM kv WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return None, 0.0
    if row is None:
        return None, 0.0
    try:
        return json.loads(row["value"]), float(row["updated_at"])
    except (ValueError, TypeError):
        return None, 0.0


# --------------------------------------------------------------------------
# Player status
# --------------------------------------------------------------------------

def set_status(status_obj: dict):
    set_state(KEY_STATUS, status_obj)
    if legacy_enabled():
        _mirror_legacy_status(status_obj)


def get_status(default=None):
    return get_state(KEY_STATUS, default if default is not None else {})


def _mirror_legacy_status(status_obj: dict):
    try:
        from fs42.station_manager import StationManager

        target = StationManager().server_conf.get("status_socket")
    except Exception:
        target = str(paths.runtime("play_status.socket"))
    if not target:
        return
    try:
        platform_compat.atomic_write_text(target, json.dumps(status_obj))
    except OSError as e:
        _l.debug("Legacy status mirror failed: %s", e)


def legacy_enabled() -> bool:
    """Whether to mirror state into the upstream runtime/*.socket files.

    On by default on Linux, where users have scripts and third-party tools
    reading those files.  Off by default on Windows, which has no such
    installed base and where the extra file handles only add failure modes.
    """
    global _legacy_enabled
    if _legacy_enabled is not None:
        return _legacy_enabled
    try:
        from fs42.station_manager import StationManager

        configured = StationManager().server_conf.get("legacy_socket_files")
    except Exception:
        configured = None
    if configured is None:
        configured = not platform_compat.IS_WINDOWS
    _legacy_enabled = bool(configured)
    return _legacy_enabled


def reset_legacy_cache():
    global _legacy_enabled
    _legacy_enabled = None


# --------------------------------------------------------------------------
# Volume (consume-once, so the OSD shows the meter exactly one time)
# --------------------------------------------------------------------------

def set_volume(volume_obj: dict):
    set_state(KEY_VOLUME, volume_obj)


def pop_volume():
    """Return the volume payload if it changed since the last call."""
    value, updated_at = get_state_with_time(KEY_VOLUME)
    if value is None:
        return None
    seen = getattr(_local, "volume_watermark", 0.0)
    if updated_at <= seen:
        return None
    _local.volume_watermark = updated_at
    return value


# --------------------------------------------------------------------------
# Event queue
# --------------------------------------------------------------------------

def push(topic: str, payload: dict):
    try:
        _connect().execute(
            "INSERT INTO events(topic, payload, created_at) VALUES(?,?,?)",
            (topic, json.dumps(payload), time.time()),
        )
        return True
    except sqlite3.Error as e:
        _l.warning("Could not push %s event: %s", topic, e)
        return False


def pop(topic: str, consumer: str = "default"):
    """Atomically claim the oldest unclaimed event on a topic."""
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "UPDATE events SET claimed_by = ? "
            "WHERE id = (SELECT id FROM events WHERE topic = ? AND claimed_by IS NULL "
            "            ORDER BY id LIMIT 1) "
            "RETURNING payload",
            (consumer, topic),
        ).fetchone()
        connection.execute("COMMIT")
    except sqlite3.Error as e:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _l.debug("Could not pop %s event: %s", topic, e)
        return None
    if row is None:
        return _pop_legacy(topic)
    try:
        return json.loads(row["payload"])
    except (ValueError, TypeError):
        return None


def _pop_legacy(topic: str):
    """Honour a command written to the upstream channel.socket file."""
    if topic != TOPIC_CHANNEL or not legacy_enabled():
        return None
    try:
        from fs42.station_manager import StationManager

        target = StationManager().server_conf.get("channel_socket")
    except Exception:
        return None
    if not target or not os.path.exists(target):
        return None
    try:
        with open(target, "r") as handle:
            contents = handle.read().strip()
        if not contents:
            return None
        with open(target, "w"):
            pass
    except OSError:
        return None
    try:
        return json.loads(contents)
    except ValueError:
        # Upstream also accepted bare text here.
        return {"command": "raw", "payload": contents}


def prune(max_age_seconds: float = 60.0):
    """Drop claimed events the janitor no longer needs."""
    try:
        _connect().execute(
            "DELETE FROM events WHERE claimed_by IS NOT NULL AND created_at < ?",
            (time.time() - max_age_seconds,),
        )
    except sqlite3.Error:
        pass


def pending_count(topic: str = None) -> int:
    try:
        if topic:
            row = _connect().execute(
                "SELECT COUNT(*) AS n FROM events WHERE topic = ? AND claimed_by IS NULL",
                (topic,),
            ).fetchone()
        else:
            row = _connect().execute(
                "SELECT COUNT(*) AS n FROM events WHERE claimed_by IS NULL"
            ).fetchone()
        return int(row["n"])
    except sqlite3.Error:
        return 0


# --------------------------------------------------------------------------
# Shutdown coordination
# --------------------------------------------------------------------------

def request_shutdown(reason: str = "requested"):
    set_state(KEY_SHUTDOWN, {"reason": reason, "at": time.time()})


def shutdown_requested() -> bool:
    return get_state(KEY_SHUTDOWN) is not None


def clear_shutdown():
    try:
        _connect().execute("DELETE FROM kv WHERE key = ?", (KEY_SHUTDOWN,))
    except sqlite3.Error:
        pass
