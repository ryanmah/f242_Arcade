from fs42.app import launch_env

STEAM_LD = ("/home/deck/.local/share/Steam/ubuntu12_32/steam-runtime/pinned_libs_32:"
            "/home/deck/.local/share/Steam/ubuntu12_32/steam-runtime/pinned_libs_64:"
            "/usr/lib/x86_64-linux-gnu/libfakeroot:"
            "/home/deck/.local/share/Steam/ubuntu12_32/steam-runtime/lib/x86_64-linux-gnu")
STEAM_PRELOAD = ("/home/deck/.local/share/Steam/ubuntu12_32/gameoverlayrenderer.so:"
                 "/home/deck/.local/share/Steam/ubuntu12_64/gameoverlayrenderer.so")


def test_steam_runtime_paths_and_overlay_are_removed():
    env, changes = launch_env.cleaned({"LD_LIBRARY_PATH": STEAM_LD, "LD_PRELOAD": STEAM_PRELOAD, "SteamAppId": "123"})
    assert env["LD_LIBRARY_PATH"] == "/usr/lib/x86_64-linux-gnu/libfakeroot"
    assert "LD_PRELOAD" not in env
    assert len(changes) == 2


def test_other_preloads_survive():
    env, _ = launch_env.cleaned({"LD_PRELOAD": "/usr/lib/libmangohud.so /x/ubuntu12_64/gameoverlayrenderer.so"})
    assert env["LD_PRELOAD"] == "/usr/lib/libmangohud.so"


def test_a_plain_desktop_launch_is_left_alone():
    base = {"DISPLAY": ":0", "LD_LIBRARY_PATH": "/opt/thing/lib", "XDG_CURRENT_DESKTOP": "KDE"}
    env, changes = launch_env.cleaned(base)
    assert env == base and changes == []


def test_gamescope_gets_x11_qt():
    env, changes = launch_env.cleaned({"XDG_CURRENT_DESKTOP": "gamescope", "WAYLAND_DISPLAY": "gamescope-0"})
    assert env["QT_QPA_PLATFORM"] == "xcb"
    assert "WAYLAND_DISPLAY" not in env
    assert launch_env.under_gamescope({"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"})


def test_packaged_app_cleans_the_callers_value(monkeypatch):
    monkeypatch.setattr(launch_env.sys, "frozen", True, raising=False)
    env, _ = launch_env.cleaned({"LD_LIBRARY_PATH": "/opt/fs42/_internal:" + STEAM_LD, "LD_LIBRARY_PATH_ORIG": STEAM_LD})
    assert "LD_LIBRARY_PATH_ORIG" not in env
    assert env["LD_LIBRARY_PATH"] == "/usr/lib/x86_64-linux-gnu/libfakeroot"


def test_gamescope_overrides_a_wayland_request():
    env, changes = launch_env.cleaned({"XDG_CURRENT_DESKTOP": "gamescope", "QT_QPA_PLATFORM": "wayland"})
    assert env["QT_QPA_PLATFORM"] == "xcb"


def test_inside_the_steam_container_libraries_are_left_alone():
    ld = "/usr/lib/pressure-vessel/overrides/lib/x86_64-linux-gnu:/x/SteamLinuxRuntime_4/lib"
    env, changes = launch_env.cleaned({"LD_LIBRARY_PATH": ld, "PRESSURE_VESSEL_RUNTIME": "1"})
    assert env["LD_LIBRARY_PATH"] == ld
    assert any("container" in c for c in changes)
