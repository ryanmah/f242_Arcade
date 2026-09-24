"""CRT scanlines and noise: settings, the mpv shader, and the menu page."""

import json
import os
import tempfile

import pytest

from fs42 import ipc, paths, video_effects


@pytest.fixture
def home(monkeypatch):
    from fs42.station_manager import StationManager

    with tempfile.TemporaryDirectory() as scratch:
        monkeypatch.setenv("FS42_HOME", scratch)
        paths.reset_cached_roots()
        StationManager._StationManager__we_are_all_one.clear()
        ipc.set_db_path(os.path.join(scratch, "state.db"))
        paths.first_run_seed()
        yield scratch
        ipc.close()
        ipc.set_db_path(None)
        paths.reset_cached_roots()
        StationManager._StationManager__we_are_all_one.clear()


def test_clean_fills_and_clamps():
    assert video_effects.clean(None) == video_effects.DEFAULTS
    v = video_effects.clean({"scanline_opacity": 7, "scanline_size": "1", "noise_opacity": -1, "noise_grain": 2.6,
                             "scanline_style": "SOFT", "scanline_pattern": "diagonal", "junk": 1})
    assert v == {"scanline_opacity": 1.0, "scanline_size": 2, "scanline_thickness": 1, "scanline_style": "soft",
                 "scanline_pattern": "horizontal", "noise_opacity": 0.0, "noise_grain": 3}
    # A line always leaves a gap: spacing grows to fit a thick line.
    assert video_effects.clean({"scanline_thickness": 5, "scanline_size": 3})["scanline_size"] == 6
    assert video_effects.is_off({"scanline_opacity": 0, "noise_opacity": 0})
    assert not video_effects.is_off({"scanline_opacity": 0.3})


def test_shader_bakes_the_numbers_in():
    src = video_effects.shader_source({"scanline_opacity": 0.4, "scanline_size": 4, "noise_opacity": 0.1, "noise_grain": 2})
    assert src.startswith("//!HOOK OUTPUT")
    assert "0.400" in src and "4.0" in src and "0.100" in src and "/ 2.0" in src
    assert "uint(frame)" in src             # noise changes every frame


def test_line_profiles_by_style():
    hard = video_effects.line_profile({"scanline_style": "hard", "scanline_thickness": 2, "scanline_size": 5})
    assert hard == [1.0, 1.0, 0.0, 0.0, 0.0]
    medium = video_effects.line_profile({"scanline_style": "medium", "scanline_thickness": 2, "scanline_size": 5})
    soft = video_effects.line_profile({"scanline_style": "soft", "scanline_thickness": 4, "scanline_size": 8})
    assert medium[:2] == [1.0, 1.0] and 0.0 < medium[2] < 1.0 and min(medium) == 0.0
    assert max(soft) > 0.95 and 0.0 < soft[0] < 0.95       # soft: no hard edge
    for profile in (hard, medium, soft):                    # the middle of the gap stays clear
        assert min(profile) < 0.06


def test_shader_follows_the_screen_style():
    base = {"scanline_opacity": 0.5}
    assert "fs42_line(px.y)" in video_effects.shader_source({**base, "scanline_pattern": "horizontal"})
    assert "fs42_line(px.x)" in video_effects.shader_source({**base, "scanline_pattern": "vertical"})
    assert "max(fs42_line(px.x), fs42_line(px.y))" in video_effects.shader_source({**base, "scanline_pattern": "grid"})
    assert "3.0 - 2.0 * x" in video_effects.shader_source({**base, "scanline_style": "soft"})
    assert "3.0 - 2.0 * x" not in video_effects.shader_source({**base, "scanline_style": "hard"})


def test_apply_to_mpv_sets_and_clears_the_shader(home):
    calls = []

    class FakeMpv:
        def command(self, *args):
            calls.append(args)

    mpv = FakeMpv()
    assert video_effects.apply_to_mpv(mpv, {"scanline_opacity": 0.3})
    assert calls[-1][:3] == ("change-list", "glsl-shaders", "set")
    shader = calls[-1][3]
    assert os.path.exists(shader) and "0.300" in open(shader).read()
    assert video_effects.apply_to_mpv(mpv, {"scanline_opacity": 0.5})
    assert calls[-1][3] != shader and not os.path.exists(shader)   # new file, old one gone
    assert video_effects.apply_to_mpv(mpv, {})
    assert calls[-1] == ("change-list", "glsl-shaders", "clr", "")
    video_effects.cleanup_shader_files()
    assert not list(paths.cache().glob("fs42_crt_*.glsl"))


def test_save_round_trips_through_main_config(home):
    from fs42.station_manager import StationManager

    video_effects.save({"scanline_opacity": 0.25, "noise_opacity": 0.1, "noise_grain": 3})
    config = json.loads(open(os.path.join(home, "confs", "main_config.json")).read())
    assert config["video_effects"]["scanline_opacity"] == 0.25 and config["video_effects"]["noise_grain"] == 3
    assert config["video_effects"]["scanline_style"] == "hard"      # defaults filled in
    StationManager._StationManager__we_are_all_one.clear()
    assert video_effects.load()["noise_grain"] == 3


def test_effects_page_dials_previews_and_saves(home):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from fs42.menu import pages
    from fs42.menu.app import _build_window

    window = _build_window()()
    page = pages.VideoEffectsPage(window)
    window.push(page)
    assert not next(r for r in page.rows if r.title == "Save").enabled

    page._start_adjust("scanline_opacity")
    page.handle("up"); page.handle("up")
    assert page.values["scanline_opacity"] == pytest.approx(0.1)
    assert ipc.pop(ipc.TOPIC_PLAYER_CMD, "t")["values"]["scanline_opacity"] == pytest.approx(0.05)
    assert window.effects.values["scanline_opacity"] == pytest.approx(0.1)
    page.handle("back")                       # cancel puts it back
    assert page.values["scanline_opacity"] == 0.0

    page._start_adjust("noise_opacity")
    page.handle("up"); page.handle("select")
    assert page.values["noise_opacity"] == pytest.approx(0.05)
    assert next(r for r in page.rows if r.title == "Save").enabled
    page._save()
    assert video_effects.load()["noise_opacity"] == pytest.approx(0.05)
    assert window.effects.timer.isActive()    # noise animates on the menu

    # Style and screen style cycle; a preset from the presets page previews
    # without the page underneath reverting it.
    page._start_adjust("scanline_pattern")
    page.handle("up")
    assert page.values["scanline_pattern"] == "vertical"
    page.handle("select")
    presets = pages.EffectPresetsPage(window, page)
    window.push(presets)
    presets._pick(dict(pages.EffectPresetsPage.PRESETS)["Heavy CRT"])
    assert window.stack[-1] is page
    assert page.values["scanline_thickness"] == 3 and window.effects.values["scanline_opacity"] == 0.75

    # Leaving with unsaved changes reverts them.
    page._apply_preset({"scanline_opacity": 0.9})
    window.pop()
    assert window.effects.values["scanline_opacity"] == 0.0
    window.close()


def test_overlay_is_skipped_without_a_compositor(monkeypatch):
    from fs42 import effects_overlay

    monkeypatch.delenv("FS42_FORCE_OVERLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(effects_overlay.os, "name", "posix")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert effects_overlay.compositing_available() is False
    monkeypatch.setenv("FS42_FORCE_OVERLAY", "1")
    assert effects_overlay.compositing_available() is True
