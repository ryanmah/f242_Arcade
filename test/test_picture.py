"""Per-channel scaling and zoom."""

import math
import os
import tempfile
from pathlib import Path

import pytest

from fs42 import ipc, paths, picture


def test_modes_round_trip_through_a_station_config():
    for values in ({"mode": "fit", "zoom": 1.0}, {"mode": "fill", "zoom": 1.2}, {"mode": "stretch", "zoom": 0.9}):
        conf = {"network_name": "X"}
        picture.write_to_station(conf, values)
        assert picture.from_station(conf) == values
    conf = {"panscan": 1.0, "video_zoom": 1.3}
    picture.write_to_station(conf, {"mode": "fit", "zoom": 1.0})
    assert conf == {}                         # back to normal leaves no keys behind


def test_upstream_keys_are_understood():
    assert picture.from_station({"video_keepaspect": False})["mode"] == "stretch"
    assert picture.from_station({"panscan": 0.5})["mode"] == "fill"
    # A hand-set partial crop survives a save in FILL mode.
    conf = {"panscan": 0.5}
    picture.write_to_station(conf, {"mode": "fill", "zoom": 1.0})
    assert conf["panscan"] == 0.5


def test_mpv_properties_and_limits():
    assert picture.clean({"mode": "odd", "zoom": 9}) == {"mode": "fit", "zoom": 2.0}
    props = picture.mpv_properties({"mode": "fill", "zoom": 2.0})
    assert props == {"keepaspect": True, "panscan": 1.0, "video_zoom": 1.0}
    assert picture.mpv_properties({"mode": "stretch", "zoom": 1.0})["keepaspect"] is False

    class FakeMpv:
        pass

    mpv = FakeMpv()
    assert picture.apply_station(mpv, {"video_zoom": 1.5})
    assert mpv.video_zoom == pytest.approx(math.log2(1.5)) and mpv.panscan == 0.0


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


def test_picture_page_previews_and_saves_to_the_station(home):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from fs42.menu import pages, station_forms
    from fs42.station_manager import StationManager

    folder = home / "catalog" / "loop"
    folder.mkdir(parents=True)
    (folder / "a.mp4").write_bytes(b"0" * 16)
    config = station_forms.build_station_config(folder, "Loop", 5, "loop", [])
    station_forms.create_station(config)

    from fs42.menu.app import _build_window

    window = _build_window()()
    page = pages.PicturePage(window, "Loop")
    window.push(page)
    page._start_adjust("zoom")
    page.handle("up"); page.handle("up"); page.handle("select")
    sent = [m for m in iter(lambda: ipc.pop(ipc.TOPIC_PLAYER_CMD, "t"), None)]
    assert sent[-1] == {"command": "picture", "network_name": "Loop", "values": {"mode": "fit", "zoom": 1.1}}
    page._start_adjust("mode")
    page.handle("up"); page.handle("select")
    page._save()
    StationManager._StationManager__we_are_all_one.clear()
    station = StationManager().station_by_name("Loop")
    assert picture.from_station(station) == {"mode": "fill", "zoom": 1.1}
    window.close()
