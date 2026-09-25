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


# ----------------------------------------------------------------- gamepad

def test_default_gamepad_mapping_covers_every_function():
    from fs42.menu import gamepad

    mapping = gamepad.default_mapping()
    assert set(mapping.values()) == set(gamepad.FUNCTION_NAMES)


def test_custom_mapping_moves_a_function_and_frees_its_old_button():
    from fs42.menu import gamepad

    default = gamepad._JoystickBackend.DEFAULT_MAP
    mapping = gamepad.apply_custom(default, {"button_3": "menu", "button_9": "bogus"})
    assert mapping["button_3"] == "menu"
    assert "button_7" not in mapping          # Start no longer opens the menu
    assert "button_9" not in mapping          # unknown functions are ignored
    assert gamepad.controls_for(mapping, "menu") == ["button_3"]


def test_old_channel_functions_become_up_and_down():
    from fs42.menu import gamepad

    mapping = gamepad.apply_custom(gamepad._JoystickBackend.DEFAULT_MAP, {"button_9": "channel_up"})
    assert mapping["button_9"] == "up"


def test_up_and_down_surf_channels_when_the_menu_is_closed(home):
    from fs42.menu import gamepad

    ipc.set_state(ipc.KEY_MENU_OPEN, False)
    ipc.set_state(ipc.KEY_INPUT_CAPTURE, False)
    gamepad._last_channel_change = 0.0
    gamepad.emit("up")
    assert ipc.pop(ipc.TOPIC_CHANNEL, "t") == {"command": "up"}
    gamepad.emit("down")                      # too soon after the last one
    assert ipc.pop(ipc.TOPIC_CHANNEL, "t") is None
    gamepad._last_channel_change = 0.0
    gamepad.emit("down")
    assert ipc.pop(ipc.TOPIC_CHANNEL, "t") == {"command": "down"}
    ipc.set_state(ipc.KEY_MENU_OPEN, True)
    gamepad.emit("up")
    assert ipc.pop(ipc.TOPIC_CHANNEL, "t") is None
    assert ipc.pop(ipc.TOPIC_MENU_INPUT, "t") == {"action": "up"}
    ipc.set_state(ipc.KEY_MENU_OPEN, False)


def test_controllers_round_trip_through_main_config(home):
    from fs42.menu import gamepad

    gamepad.save_controllers([
        {"name": "Stick", "device": "DragonRise", "map": {"button_2": "select", "button_0": "back", "button_8": "channel_up"}},
        {"name": "Pad", "device": "*", "map": {"button_3": "menu"}},
    ])
    config = json.loads((home / "confs" / "main_config.json").read_text())
    assert config["gamepad"] is True
    assert config["controllers"][0]["map"] == {"button_2": "select", "button_0": "back", "button_8": "up"}
    table = gamepad.MappingTable()
    stick = table.for_device("DragonRise")
    assert stick["button_2"] == "select" and stick["button_0"] == "back"
    assert "button_1" not in stick            # B used to be back; the user moved it
    other = table.for_device("Some other pad")  # falls back to the catch-all
    assert other["button_3"] == "menu" and "button_7" not in other


def test_old_flat_gamepad_map_reads_as_a_catch_all_controller(home):
    from fs42.menu import gamepad

    path = home / "confs" / "main_config.json"
    path.write_text(json.dumps({"gamepad": True, "gamepad_map": {"button_9": "channel_down"}}))
    controllers = gamepad.load_controllers()
    assert controllers == [{"name": "Controller", "device": "*", "map": {"button_9": "down"}}]
    assert gamepad.load_mapping("anything")["button_9"] == "down"


def test_repeater_maps_controls_and_repeats_directions():
    from fs42.menu import gamepad

    seen = []
    table = {"pad": {"button_0": "select", "axis_1_neg": "up"}}
    repeater = gamepad._Repeater(lambda d: table.get(d, {}), sink=seen.append)
    repeater.update({("pad", "button_0"), ("pad", "axis_1_neg"), ("other", "button_0")}, 0.0)
    assert sorted(seen) == ["select", "up"]
    repeater.update({("pad", "button_0"), ("pad", "axis_1_neg")}, gamepad.REPEAT_DELAY + 0.01)
    assert seen.count("up") == 2 and seen.count("select") == 1
    repeater.update(set(), 1.0)
    assert repeater.held == {}


def test_emit_is_muted_while_the_add_input_page_listens(home):
    from fs42.menu import gamepad

    ipc.set_state(ipc.KEY_MENU_OPEN, False)
    ipc.set_state(ipc.KEY_INPUT_CAPTURE, True)
    gamepad.emit("menu")
    assert ipc.pop(ipc.TOPIC_PLAYER_CMD, "t") is None
    ipc.set_state(ipc.KEY_INPUT_CAPTURE, False)
    gamepad.emit("menu")
    assert ipc.pop(ipc.TOPIC_PLAYER_CMD, "t") == {"command": "menu"}


def test_describe_control_is_readable():
    from fs42.menu import gamepad

    assert gamepad.describe_control("dpad_up") == "D-PAD UP"
    assert gamepad.describe_control("axis_7_neg") == "AXIS 7 -"
    assert gamepad.describe_control("lstick_left") == "L-STICK LEFT"
    assert gamepad.describe_control("a") == "A"


# ------------------------------------------------------------------- pages

@pytest.fixture
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_menu_font_loads(qt_app):
    from fs42.menu import theme

    assert theme.FONT_FILE.exists()
    assert theme.load_fonts() == "VCR OSD Mono"


def test_home_page_has_the_new_entries(home, qt_app):
    from fs42.menu.app import _build_window

    window = _build_window()()
    titles = [row.title for row in window.stack[-1].rows]
    assert "Open web portal" in titles
    assert "Remote Controls" in titles
    assert "Shutdown FS42" in titles
    assert "Exit menu" in titles
    assert any(t.startswith("View: ") for t in titles)
    window.close()


def test_add_input_pages_add_map_and_delete_a_controller(home, qt_app):
    from fs42.menu import gamepad, pages
    from fs42.menu.app import _build_window

    window = _build_window()()
    window.push(pages.ControllersPage(window))
    assert [r.title for r in window.stack[-1].rows] == ["+  Add a controller"]

    page = pages.InputPage(window)
    window.push(page)
    assert ipc.get_state(ipc.KEY_INPUT_CAPTURE) is True
    assert not page.device_known and all(not r.enabled for r in page.rows if r.title != "Cancel")

    # The first press names the controller; then a function is learned.
    page._raw("Arcade Stick", "button_5")
    page._poll()
    assert page.entry["device"] == "Arcade Stick" and page.entry["name"] == "Arcade Stick"
    page._capture("select")
    page._raw("Some Other Pad", "button_3")      # ignored: a different device
    page._poll()
    assert page.capturing == "select"
    page._raw("Arcade Stick", "button_3")
    page._poll()
    assert page.entry["map"] == {"button_3": "select"} and page.capturing is None

    page._save()
    saved = gamepad.load_controllers()
    assert saved == [{"name": "Arcade Stick", "device": "Arcade Stick", "map": {"button_3": "select"}}]
    assert ipc.pop(ipc.TOPIC_PLAYER_CMD, "t") == {"command": "reload_input"}
    window.pop()
    assert ipc.get_state(ipc.KEY_INPUT_CAPTURE) is False

    # It is listed now, and can be deleted from its own page (two steps).
    listing = pages.ControllersPage(window)
    window.push(listing)
    assert [r.title for r in listing.rows][0] == "Arcade Stick"
    edit = pages.InputPage(window, controller_index=0)
    window.push(edit)
    assert edit.device_known and edit.title == "Arcade Stick"
    edit._ask_delete()
    assert any("Really delete" in r.title for r in edit.rows)
    edit._delete()
    assert gamepad.load_controllers() == []
    window.close()


def test_quit_page_asks_the_supervisor_to_stop(home, qt_app):
    from fs42.menu import pages
    from fs42.menu.app import _build_window

    window = _build_window()()
    page = pages.ConfirmQuitPage(window)
    window.push(page)
    ipc.clear_shutdown()
    page._quit()
    assert ipc.shutdown_requested()
    ipc.clear_shutdown()
    window.close()


# -------------------------------------------------------------- guide paths

@pytest.fixture
def tk_or_stub(monkeypatch):
    """guide_tk needs tkinter to import; stand in a stub where it is absent."""
    import sys as _sys
    import types

    try:
        import tkinter  # noqa: F401
        return
    except ImportError:
        pass
    stub = types.ModuleType("tkinter")
    for name in ("Tk", "Frame", "Canvas", "Label"):
        setattr(stub, name, type(name, (), {}))
    stub.NW = "nw"
    monkeypatch.setitem(_sys.modules, "tkinter", stub)
    import PIL

    if not hasattr(PIL, "ImageTk"):
        monkeypatch.setitem(_sys.modules, "PIL.ImageTk", types.ModuleType("PIL.ImageTk"))
        monkeypatch.setattr(PIL, "ImageTk", _sys.modules["PIL.ImageTk"], raising=False)
    _sys.modules.pop("fs42.guide_tk", None)


def test_guide_channel_with_missing_artwork_does_not_kill_the_app(home, tk_or_stub):
    from fs42.station_manager import StationManager

    (home / "confs" / "guide.json").write_text(json.dumps({"station_conf": {
        "network_name": "Guide", "channel_number": 1, "network_type": "guide",
        "images": ["docs/logo_images/nope.png", "runtime/logo_images/gold42.png"],
        "sound_to_play": "runtime/missing.mp3", "play_sound": True,
    }}))
    StationManager._StationManager__we_are_all_one.clear()
    manager = StationManager()          # used to exit(-1) here
    guide = manager.station_by_name("Guide")
    assert guide is not None and guide["_guide_problems"]
    assert any("nope.png" in p for p in guide["_guide_problems"])


def test_guide_files_resolve_against_the_data_folder_and_the_bundle(home, tk_or_stub):
    from fs42 import guide_tk

    seeded = home / "runtime" / "logo_images" / "gold42.png"
    assert seeded.exists()
    assert guide_tk._locate_guide_file("runtime/logo_images/gold42.png") == seeded
    # Upstream-style path from a repo checkout: found in the seeded copy.
    assert guide_tk._locate_guide_file("docs/logo_images/gold42.png").exists()
    # Absolute paths are honoured as written, even when missing.
    missing = str(home / "nowhere.png")
    assert str(guide_tk._locate_guide_file(missing)) == missing


def test_osd_font_tag_accepts_family_or_file():
    from fs42.osd.backends.mpv_osd import _font_family
    from fs42.osd.config import StatusDisplayConfig

    assert _font_family("VCR OSD Mono") == "VCR OSD Mono"
    assert _font_family("/fonts/Impact.ttf") == "Impact"
    assert StatusDisplayConfig().font == "VCR OSD Mono"


def test_menu_fonts_dir_holds_only_fonts():
    from fs42 import paths

    names = sorted(p.name for p in paths.menu_fonts_dir().iterdir())
    assert names == ["VCR_OSD_MONO.ttf"]      # libass loads every file in here


# ------------------------------------------------------------ osd position

def test_channel_banner_sits_in_the_4x3_picture():
    from fs42.osd.backends import mpv_osd
    from fs42.osd.config import HAlignment, StatusDisplayConfig

    config = StatusDisplayConfig()                       # LEFT, anchor 4:3, 30 px
    # 16:9 window: the 4:3 picture starts 240 canvas units in; +30 px.
    assert mpv_osd._status_x(config, 16 / 9) == pytest.approx(270.0)
    # A 4:3 screen: the picture is the whole screen; 30 px at 1080p is
    # 40 canvas units because the canvas is stretched over a narrower window.
    assert mpv_osd._status_x(config, 4 / 3) == pytest.approx(40.0)
    # Right-aligned mirrors it.
    right = StatusDisplayConfig(halign=HAlignment.RIGHT)
    assert mpv_osd._status_x(right, 16 / 9) == pytest.approx(1920 - 270.0)
    # The old screen-relative placement is still available.
    screen = StatusDisplayConfig(anchor="screen")
    assert mpv_osd._status_x(screen, 16 / 9) == pytest.approx(0.12 / 2 * 1920)


def test_view_toggle_saves_and_tells_the_player(home, qt_app):
    from fs42.menu import pages
    from fs42.menu.app import _build_window

    window = _build_window()()
    page = window.stack[-1]
    assert "View: Fullscreen" in [r.title for r in page.rows]
    page._toggle_view()
    config = json.loads((home / "confs" / "main_config.json").read_text())
    assert config["fullscreen"] is False
    assert ipc.pop(ipc.TOPIC_PLAYER_CMD, "t") == {"command": "view", "fullscreen": False}
    assert "View: Windowed" in [r.title for r in page.rows]
    window.close()


def test_stations_page_opens_at_once_then_fills_in(home, qt_app):
    import time as _time

    from PySide6.QtWidgets import QApplication

    from fs42.menu import pages, station_forms
    from fs42.menu.app import _build_window

    folder = home / "catalog" / "loop"
    folder.mkdir(parents=True)
    (folder / "a.mp4").write_bytes(b"0" * 16)
    station_forms.create_station(station_forms.build_station_config(folder, "Loop", 5, "loop", []))

    window = _build_window()()
    page = pages.StationsPage(window)
    window.push(page)
    assert [r.title for r in page.rows] == ["Loading..."]
    deadline = _time.time() + 5
    while page._summaries is None and _time.time() < deadline:
        QApplication.processEvents()
        _time.sleep(0.05)
    titles = [r.title for r in page.rows]
    assert any("Loop" in t for t in titles) and titles[-1].endswith("Add a station")
    window.close()


def test_startup_fade_covers_then_lifts():
    from fs42.osd.backends import mpv_osd

    class FakeMpv:
        def __init__(self):
            self.payloads = []

        def command(self, *args):
            if args[0] == "osd-overlay":
                self.payloads.append(args[3])

    mpv = FakeMpv()
    backend = mpv_osd.MpvOSD.__new__(mpv_osd.MpvOSD)
    backend.mpv = mpv
    backend.status_elements, backend.volume_elements, backend.logo_elements = [], [], []
    backend._last_payload = None
    backend.cover()
    assert "1a&H00&" in mpv.payloads[-1]                  # opaque black
    backend.fade_in(0.2)
    import time as _time
    _time.sleep(0.25)
    backend._draw_ass()
    assert mpv.payloads[-1] == ""                        # cover gone


def test_captions_toggle_saves_and_tells_the_player(home, qt_app):
    from fs42.menu.app import _build_window

    window = _build_window()()
    page = window.stack[-1]
    assert "Captions: Off" in [r.title for r in page.rows]
    page._toggle_captions()
    config = json.loads((home / "confs" / "main_config.json").read_text())
    assert config["captions"] is True
    assert ipc.pop(ipc.TOPIC_PLAYER_CMD, "t") == {"command": "captions", "on": True}
    assert "Captions: On" in [r.title for r in page.rows]
    window.close()


def test_caption_options_turn_every_track_off():
    from fs42 import station_player

    off = station_player._caption_options(False)
    assert off == {"sid": "no", "sub_auto": "no", "sub_visibility": False}
    assert station_player._caption_options(True)["sid"] == "auto"
