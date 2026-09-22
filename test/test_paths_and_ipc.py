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
