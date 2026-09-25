"""Tests for the fork's path resolution and cross-process state bus."""

import json
import multiprocessing
import os
import tempfile
import time
from pathlib import Path

import pytest

from fs42 import ipc, paths


@pytest.fixture
def fs42_home(monkeypatch):
    from fs42.station_manager import StationManager

    with tempfile.TemporaryDirectory() as scratch:
        monkeypatch.setenv("FS42_HOME", scratch)
        paths.reset_cached_roots()
        # StationManager is a borg singleton: every instance shares one dict,
        # and it caches absolute paths at construction. Clearing that shared
        # dict is the only way to make it re-read a changed data root.
        StationManager._StationManager__we_are_all_one.clear()
        ipc.set_db_path(os.path.join(scratch, "state.db"))
        ipc.reset_legacy_cache()
        yield Path(scratch)
        ipc.close()
        ipc.set_db_path(None)
        paths.reset_cached_roots()
        StationManager._StationManager__we_are_all_one.clear()


def test_data_root_follows_env(fs42_home):
    assert paths.data() == fs42_home


def test_relative_config_paths_resolve_against_data(fs42_home):
    # Existing configs say "catalog/nbc_catalog"; that has to keep working
    # without the process being launched from the repo root.
    assert paths.resolve_user_path("catalog/nbc") == fs42_home / "catalog" / "nbc"


def test_absolute_config_paths_are_left_alone(fs42_home):
    absolute = Path(tempfile.gettempdir()) / "somewhere"
    assert paths.resolve_user_path(str(absolute)) == absolute


def test_first_run_seed_is_idempotent(fs42_home):
    assert paths.first_run_seed() is True
    marker = paths.confs("main_config.json")
    marker.write_text('{"server_port": 9999}')
    assert paths.first_run_seed() is False
    assert json.loads(marker.read_text())["server_port"] == 9999


def test_first_run_seed_creates_the_tree(fs42_home):
    paths.first_run_seed()
    for relative in ("confs", "confs/examples", "runtime", "catalog", "osd", "static"):
        assert (fs42_home / relative).is_dir(), relative
    assert list(paths.confs("examples").glob("*.json"))


def test_state_survives_a_reconnect(fs42_home):
    ipc.init_db()
    ipc.set_state(ipc.KEY_CHANNEL_INDEX, 4)
    ipc.close()
    assert ipc.get_state(ipc.KEY_CHANNEL_INDEX) == 4


def test_events_are_claimed_exactly_once(fs42_home):
    ipc.init_db()
    ipc.push(ipc.TOPIC_CHANNEL, {"command": "up"})
    assert ipc.pop(ipc.TOPIC_CHANNEL, "a") == {"command": "up"}
    assert ipc.pop(ipc.TOPIC_CHANNEL, "b") is None


def test_events_are_first_in_first_out(fs42_home):
    ipc.init_db()
    for number in range(3):
        ipc.push(ipc.TOPIC_CHANNEL, {"command": "direct", "channel": number})
    seen = [ipc.pop(ipc.TOPIC_CHANNEL, "p")["channel"] for _ in range(3)]
    assert seen == [0, 1, 2]


def test_volume_pops_once_per_change(fs42_home):
    ipc.init_db()
    ipc.set_volume({"level": 50})
    assert ipc.pop_volume() == {"level": 50}
    assert ipc.pop_volume() is None
    time.sleep(0.01)
    ipc.set_volume({"level": 55})
    assert ipc.pop_volume() == {"level": 55}


def test_legacy_status_file_is_mirrored(fs42_home, monkeypatch):
    paths.first_run_seed()
    ipc.init_db()
    monkeypatch.setattr(ipc, "legacy_enabled", lambda: True)
    ipc.set_status({"status": "playing", "network_name": "NBC"})
    mirrored = paths.runtime("play_status.socket")
    assert json.loads(mirrored.read_text())["network_name"] == "NBC"


def _hammer(db_path, count):
    ipc.set_db_path(db_path)
    for _ in range(count):
        ipc.push(ipc.TOPIC_CHANNEL, {"command": "up"})
    ipc.close()


def test_concurrent_writers_lose_nothing(fs42_home):
    """Four processes pushing at once - the failure this replaced."""
    ipc.init_db()
    db_path = ipc.db_path()
    ipc.close()

    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_hammer, args=(db_path, 25)) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
        assert worker.exitcode == 0

    ipc.set_db_path(db_path)
    claimed = 0
    while ipc.pop(ipc.TOPIC_CHANNEL, "drain") is not None:
        claimed += 1
    assert claimed == 100


# ------------------------------------------------------- data root override

def test_data_root_follows_launcher_settings(tmp_path, monkeypatch):
    from fs42 import paths

    default = tmp_path / "default"
    other = tmp_path / "external"
    default.mkdir()
    other.mkdir()
    monkeypatch.delenv("FS42_HOME", raising=False)
    monkeypatch.setenv("FS42_DEFAULT_HOME", str(default))
    paths.reset_cached_roots()

    info = paths.data_root_info()
    assert info["source"] == "default" and info["configured"] is None
    assert paths.data() == default.resolve()

    paths.set_data_root(other)
    assert (default / "launcher.json").exists()
    info = paths.data_root_info()
    assert info["configured"] == str(other) and info["restart_required"] is True
    assert paths.data() == default.resolve()  # unchanged until restart

    paths.reset_cached_roots()
    assert paths.data() == other.resolve()
    assert paths.data_root_info()["source"] == "settings"

    # A missing drive falls back to the default rather than failing.
    other.rmdir()
    paths.reset_cached_roots()
    assert paths.data() == default.resolve()
    assert paths.data_root_info()["configured_exists"] is False

    paths.set_data_root(None)
    assert "data_root" not in paths.read_launcher_settings()
    paths.reset_cached_roots()


def test_inspect_data_root_reports_layout(tmp_path):
    from fs42 import paths

    missing = paths.inspect_data_root(tmp_path / "nope")
    assert not missing["exists"] and missing["problems"]

    empty = tmp_path / "empty"
    empty.mkdir()
    assert paths.inspect_data_root(empty)["empty"] is True

    full = tmp_path / "full"
    (full / "confs").mkdir(parents=True)
    (full / "catalog").mkdir()
    (full / "confs" / "a.json").write_text("{}")
    (full / "confs" / "main_config.json").write_text("{}")
    info = paths.inspect_data_root(full)
    assert info["looks_like_fs42"] and info["station_configs"] == 1 and info["has_catalog"] and info["writable"]

    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "holiday.jpg").write_text("x")
    assert paths.inspect_data_root(junk)["problems"]


def test_restart_request_round_trip(tmp_path):
    from fs42 import ipc

    ipc.set_db_path(tmp_path / "bus.db")
    try:
        assert ipc.restart_requested() is None
        ipc.request_restart("settings")
        assert ipc.restart_requested()["reason"] == "settings"
        ipc.clear_restart()
        assert ipc.restart_requested() is None
    finally:
        ipc.set_db_path(None)


def test_locate_media_finds_files_moved_with_the_data_folder(tmp_path, monkeypatch):
    from fs42 import paths

    monkeypatch.setenv("FS42_HOME", str(tmp_path))
    paths.reset_cached_roots()
    paths._relocated.clear()
    media = tmp_path / "catalog" / "NickTV" / "show" / "ep1.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"0")
    try:
        # As-is, relative to the data folder, and a Pi's absolute path.
        assert paths.locate_media(str(media)) == str(media)
        assert paths.locate_media("catalog/NickTV/show/ep1.mp4") == str(media)
        assert paths.locate_media("/home/pi/FieldStation42/catalog/NickTV/show/ep1.mp4") == str(media)
        assert paths.locate_media("C:\\Users\\bob\\FieldStation42\\catalog\\NickTV\\show\\ep1.mp4") == str(media)
        # Unknown files come back untouched so the error names them.
        assert paths.locate_media("/home/pi/FieldStation42/catalog/NickTV/show/gone.mp4").endswith("gone.mp4")
        assert paths.locate_media(None) is None
    finally:
        paths.reset_cached_roots()
        paths._relocated.clear()


def test_station_config_overwrites_on_windows_semantics(tmp_path, monkeypatch):
    """os.rename refuses to overwrite on Windows ([WinError 183]); writes must not use it."""
    import json
    import os

    from fs42.station_io import StationIO

    def windows_rename(src, dst):
        if os.path.exists(dst):
            raise FileExistsError(183, "Cannot create a file when that file already exists")
        return real_rename(src, dst)

    real_rename = os.rename
    monkeypatch.setattr(os, "rename", windows_rename)
    target = tmp_path / "KTLA.json"
    target.write_text(json.dumps({"station_conf": {"network_name": "KTLA"}}))
    ok, message = StationIO.__new__(StationIO).__class__.write_station_config(
        type("IO", (), {"_l": __import__("logging").getLogger("t")})(), str(target), {"station_conf": {"network_name": "KTLA", "hidden": True}})
    assert ok, message
    assert json.loads(target.read_text())["station_conf"]["hidden"] is True
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".fs42-*"))


def test_atomic_write_retries_while_a_reader_holds_the_file(tmp_path, monkeypatch):
    import os

    from fs42 import platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    calls = {"n": 0}
    real_replace = os.replace

    def busy_then_free(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", busy_then_free)
    target = tmp_path / "x.json"
    target.write_text("old")
    platform_compat.atomic_write_text(target, "new")
    assert target.read_text() == "new" and calls["n"] == 3
