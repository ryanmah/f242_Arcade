"""Warming the OS cache for what each channel plays next."""

import os
import types

from fs42 import prewarm


def test_warm_file_reads_head_and_tail_without_loading_it_all(tmp_path):
    big = tmp_path / "movie.mp4"
    big.write_bytes(os.urandom(64 * 1024) + b"\0" * (8 * 1024 * 1024) + os.urandom(64 * 1024))
    assert prewarm.warm_file(str(big), head=1024 * 1024, tail=1024 * 1024)
    assert not prewarm.warm_file(str(tmp_path / "gone.mp4"))


def test_prewarmer_warms_each_upcoming_file_once(monkeypatch, tmp_path):
    files = []
    for name in ("a.mp4", "b.mp4"):
        f = tmp_path / name
        f.write_bytes(b"x" * 4096)
        files.append(str(f))
    monkeypatch.setattr(prewarm, "upcoming_files", lambda: files)
    read = []
    monkeypatch.setattr(prewarm, "warm_file", lambda p, **kw: read.append(p) or True)
    warmer = prewarm.Prewarmer()
    assert warmer.once() == 2
    assert warmer.once() == 0          # already warm; nothing re-read
    assert read == files


def test_upcoming_files_follow_the_play_point(monkeypatch):
    entry = lambda p: types.SimpleNamespace(path=p, is_stream=False)
    point = types.SimpleNamespace(index=1, plan=[entry("/x/0.mp4"), entry("/x/1.mp4"), entry("/x/2.mp4"), entry("/x/3.mp4")])

    class FakeLiquid:
        def get_play_point(self, name, now):
            if name == "Broken":
                raise RuntimeError("no schedule")
            return point

    class FakeManager:
        stations = [{"network_name": "Retro", "network_type": "standard"},
                    {"network_name": "Broken", "network_type": "loop"},
                    {"network_name": "Guide", "network_type": "guide"}]

    import fs42.liquid_manager, fs42.station_manager
    monkeypatch.setattr(fs42.liquid_manager, "LiquidManager", FakeLiquid)
    monkeypatch.setattr(fs42.station_manager, "StationManager", FakeManager)
    assert prewarm.upcoming_files() == ["/x/1.mp4", "/x/2.mp4"]
