"""Tests for the in-app menu's non-GUI logic."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from fs42 import ipc, paths
from fs42.menu import input as menu_input
from fs42.menu import station_forms


@pytest.fixture
def home(monkeypatch):
    from fs42.station_manager import StationManager

    with tempfile.TemporaryDirectory() as scratch:
        monkeypatch.setenv("FS42_HOME", scratch)
        paths.reset_cached_roots()
        StationManager._StationManager__we_are_all_one.clear()
        ipc.set_db_path(os.path.join(scratch, "state.db"))
        paths.first_run_seed()
        yield Path(scratch)
        ipc.close()
        ipc.set_db_path(None)
        paths.reset_cached_roots()
        StationManager._StationManager__we_are_all_one.clear()


def _touch_media(folder: Path, name="a.mp4"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"\x00" * 16)


# ------------------------------------------------------------------- input

def test_every_mpv_key_maps_to_a_known_action():
    for key, action in menu_input.MPV_KEY_ACTIONS.items():
        assert menu_input.is_action(action), key


def test_digit_actions_round_trip():
    assert menu_input.digit(7) == "digit_7"
    assert menu_input.digit_value("digit_7") == 7
    assert menu_input.digit_value("up") is None
    assert menu_input.is_action("digit_0") and not menu_input.is_action("digit_x")


def test_qt_keys_map_like_mpv_keys():
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt

    assert menu_input.qt_key_to_action(Qt.Key_Escape) == menu_input.Action.BACK.value
    assert menu_input.qt_key_to_action(Qt.Key_Return) == menu_input.Action.SELECT.value
    assert menu_input.qt_key_to_action(Qt.Key_5) == "digit_5"
    assert menu_input.qt_key_to_action(Qt.Key_A) is None


# ------------------------------------------------------------ folder shapes

def test_folder_with_show_subfolders_is_standard(home):
    root = paths.catalog_root() / "kids"
    _touch_media(root / "cartoons")
    _touch_media(root / "sitcoms")
    _touch_media(root / "bumps")
    info = station_forms.describe_folder(root)
    assert info["kind"] == "standard"
    assert info["tags"] == ["cartoons", "sitcoms"]      # bumps is filler, not a show
    assert info["bump_dir"] == "bumps"
    assert info["commercial_dir"] is None


def test_folder_of_loose_files_is_loop(home):
    root = paths.catalog_root() / "music"
    _touch_media(root, "v.mp4")
    info = station_forms.describe_folder(root)
    assert info["kind"] == "loop"
    assert info["loose_files"] == 1


def test_empty_folder_is_nothing(home):
    root = paths.catalog_root() / "empty"
    root.mkdir(parents=True)
    assert station_forms.describe_folder(root)["kind"] is None


# ------------------------------------------------------------------ configs

def test_standard_config_covers_every_hour_of_every_day(home):
    root = paths.catalog_root() / "kids"
    _touch_media(root / "cartoons")
    config = station_forms.build_station_config(root, "Kids", 5, "standard", ["cartoons"])
    conf = config["station_conf"]
    assert conf["network_type"] == "standard"
    assert conf["content_dir"] == "catalog/kids"          # relative: inside the data folder
    for day in station_forms.DAYS:
        assert conf[day] == "daily"
    assert set(conf["day_templates"]["daily"]) == {str(h) for h in range(24)}
    assert conf["schedule_increment"] == 0                 # no filler folders -> back to back


def test_bumps_folder_enables_half_hour_scheduling(home):
    root = paths.catalog_root() / "kids"
    _touch_media(root / "cartoons")
    _touch_media(root / "bumps")
    conf = station_forms.build_station_config(root, "Kids", 5, "standard", ["cartoons"])["station_conf"]
    assert conf["schedule_increment"] == 30
    assert conf["commercial_free"] is True
    assert conf["bump_dir"] == "catalog/kids/bumps"


def test_folder_outside_data_root_stays_absolute(home):
    with tempfile.TemporaryDirectory() as elsewhere:
        _touch_media(Path(elsewhere), "v.mp4")
        conf = station_forms.build_station_config(elsewhere, "Out", 6, "loop")["station_conf"]
        assert Path(conf["content_dir"]).is_absolute()


def test_verify_rejects_a_missing_folder(home):
    bad = {"station_conf": {"network_name": "Bad", "channel_number": 9, "content_dir": "catalog/nope"}}
    with pytest.raises(station_forms.FormError):
        station_forms.verify_loadable(bad)


def test_verify_rejects_standard_without_days(home):
    """This exact config would exit(-1) the player on reload under upstream."""
    root = paths.catalog_root() / "kids"
    _touch_media(root / "cartoons")
    bad = {"station_conf": {"network_name": "NoDays", "channel_number": 9, "content_dir": "catalog/kids"}}
    with pytest.raises(station_forms.FormError):
        station_forms.verify_loadable(bad)


def test_create_hide_delete_round_trip(home):
    from fs42.station_manager import StationManager

    root = paths.catalog_root() / "music"
    _touch_media(root, "v.mp4")
    config = station_forms.build_station_config(root, "Music", 6, "loop")
    station_forms.create_station(config)
    assert (paths.confs() / "music.json").exists()
    assert StationManager().station_by_name("Music") is not None

    with pytest.raises(station_forms.FormError):
        station_forms.create_station(config)             # duplicate name

    station_forms.set_hidden("Music", True)
    assert json.loads((paths.confs() / "music.json").read_text())["station_conf"]["hidden"] is True

    station_forms.delete_station(StationManager().station_by_name("Music"))
    assert not (paths.confs() / "music.json").exists()
    assert StationManager().station_by_name("Music") is None


def test_next_channel_number_skips_used_ones():
    stations = [{"channel_number": 2}, {"channel_number": 3}, {"channel_number": 5}]
    assert station_forms.next_channel_number(stations) == 4


def test_reload_if_changed_notices_edits(home):
    from fs42.station_manager import StationManager

    manager = StationManager()
    assert manager.reload_if_changed() is False
    root = paths.catalog_root() / "music"
    _touch_media(root, "v.mp4")
    (paths.confs() / "zz.json").write_text(json.dumps(
        {"station_conf": {"network_name": "ZZ", "channel_number": 77, "network_type": "loop", "content_dir": "catalog/music"}}
    ))
    assert manager.reload_if_changed() is True
    assert manager.station_by_name("ZZ") is not None
    assert manager.reload_if_changed() is False
