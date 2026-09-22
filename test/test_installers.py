"""Tests for the installer helpers: the Steam shortcut writer, the binary
fetcher's helpers, and the shape of the Linux installer scripts."""

import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LINUX = ROOT / "packaging" / "linux"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


steam_shortcut = _load(LINUX / "steam_shortcut.py", "steam_shortcut")
fetch_binaries = _load(ROOT / "packaging" / "fetch_binaries.py", "fetch_binaries")


# ------------------------------------------------------------- binary VDF

def test_vdf_round_trip():
    entry = steam_shortcut.make_entry("/opt/app/App", "/opt/app", "/opt/app/icon.png", "--flag")
    root = {"shortcuts": {"0": entry, "1": {**entry, "AppName": "Other", "tags": {"0": "favorite"}}}}
    blob = steam_shortcut.dump(root)
    assert blob.startswith(b"\x00shortcuts\x00\x00" + b"0\x00\x02appid\x00")
    assert blob.endswith(b"\x08\x08")
    assert steam_shortcut.parse(blob) == root


def test_vdf_appid_is_unsigned_with_top_bit_and_survives_the_trip():
    appid = steam_shortcut.app_id('"/x/App"', "FieldStation42")
    assert appid & 0x80000000
    blob = steam_shortcut.dump({"a": appid})
    # Stored as 4 little-endian bytes exactly as Steam writes them.
    assert struct.unpack_from("<I", blob, len(b"\x02a\x00"))[0] == appid
    assert steam_shortcut.parse(blob)["a"] & 0xFFFFFFFF == appid


def test_vdf_rejects_unknown_types():
    with pytest.raises(ValueError):
        steam_shortcut.parse(b"\x07key\x00\x08")


# ---------------------------------------------------------- add / remove

@pytest.fixture
def steam_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config = tmp_path / ".local/share/Steam/userdata/1001/config"
    config.mkdir(parents=True)
    existing = steam_shortcut.make_entry("/usr/bin/foo", "/usr/bin", "", "")
    existing["AppName"] = "Existing Game"
    (config / "shortcuts.vdf").write_bytes(steam_shortcut.dump({"shortcuts": {"0": existing}}))
    return config / "shortcuts.vdf"


def _names(path):
    return [e["AppName"] for e in steam_shortcut.load(path)["shortcuts"].values()]


def test_add_appends_then_replaces_then_removes(steam_home, tmp_path):
    exe = tmp_path / "app" / "FieldStation42"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")

    assert steam_shortcut.add(str(exe), "/icon.png", "") == 0
    assert _names(steam_home) == ["Existing Game", "FieldStation42"]
    entry = steam_shortcut.load(steam_home)["shortcuts"]["1"]
    assert entry["Exe"] == f'"{exe}"'
    assert entry["StartDir"] == f'"{exe.parent}"'
    assert entry["icon"] == "/icon.png"
    assert steam_home.with_suffix(".vdf.fs42-backup").exists()

    # Second add updates in place - no duplicates.
    assert steam_shortcut.add(str(exe), "/other.png", "--x") == 0
    assert _names(steam_home) == ["Existing Game", "FieldStation42"]
    assert steam_shortcut.load(steam_home)["shortcuts"]["1"]["LaunchOptions"] == "--x"

    assert steam_shortcut.remove() == 0
    assert _names(steam_home) == ["Existing Game"]


def test_add_creates_file_for_fresh_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config = tmp_path / ".steam/steam/userdata/77/config"
    config.mkdir(parents=True)
    (tmp_path / ".steam/steam/userdata/0").mkdir()  # the anonymous profile is skipped
    assert steam_shortcut.add("/opt/App", "", "") == 0
    assert _names(config / "shortcuts.vdf") == ["FieldStation42"]


def test_add_without_steam_reports_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert steam_shortcut.add("/opt/App", "", "") == 1
    assert "No Steam user profiles" in capsys.readouterr().err


def test_cli_list_and_check_running(steam_home, monkeypatch, capsys):
    monkeypatch.setattr(steam_shortcut, "steam_is_running", lambda: True)
    assert steam_shortcut.main(["--remove", "--check-running"]) == 3
    assert steam_shortcut.main(["--list"]) == 0
    assert "Existing Game" in capsys.readouterr().out


# ---------------------------------------------------------- fetch_binaries

def test_lockfile_is_fully_pinned():
    lock = json.loads((ROOT / "packaging" / "binaries.lock.json").read_text())
    for platform, entries in lock["platforms"].items():
        for name, spec in entries.items():
            assert spec["url"].startswith("https://"), (platform, name)
            assert spec.get("sha256") and len(spec["sha256"]) == 64, (platform, name)
            assert spec["archive"] in ("zip", "7z", "tar.gz", "tar.xz", "appimage"), (platform, name)
            if spec["archive"] != "appimage":
                assert spec["extract"], (platform, name)
            # Rolling tags need a way to move the pin; pruned nightlies need a fallback.
            assert spec.get("rolling") or spec.get("release"), (platform, name)


def test_extract_patterns_match_basenames_and_globs():
    assert fetch_binaries._matches("ffmpeg-x/bin/ffprobe.exe", ["**/bin/ffprobe.exe"])
    assert fetch_binaries._matches("mpv.exe", ["mpv.exe", "*.dll"])
    assert not fetch_binaries._matches("doc/mpv.html", ["mpv.exe"])


def test_extract_files_copies_only_matches_and_sets_exec(tmp_path):
    import zipfile

    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("build/bin/ffprobe", "#!/bin/sh\n")
        zf.writestr("build/bin/ffplay", "#!/bin/sh\n")
        zf.writestr("build/LICENSE", "x")
    out = tmp_path / "vendor"
    names = fetch_binaries.extract_files(archive, "zip", ["**/bin/ffprobe"], out)
    assert names == ["ffprobe"]
    assert (out / "ffprobe").exists() and not (out / "ffplay").exists()
    if os.name != "nt":
        assert os.access(out / "ffprobe", os.X_OK)


def test_wrapper_script_execs_apprun():
    text = fetch_binaries.WRAPPER.format(appdir="mpv.AppDir")
    assert text.startswith("#!/bin/sh")
    assert 'exec "$APPDIR/AppRun" "$@"' in text


# ------------------------------------------------------ installer scripts

@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
def test_shell_scripts_parse(script):
    subprocess.run(["sh", "-n", str(LINUX / script)], check=True)


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_install_sh_help_and_refusal(tmp_path):
    result = subprocess.run(["sh", str(LINUX / "install.sh"), "--help"], capture_output=True, text=True)
    assert result.returncode == 0 and "--steam" in result.stdout
    result = subprocess.run(["sh", str(LINUX / "install.sh"), "--yes"], capture_output=True, text=True,
                            cwd=tmp_path, env={**os.environ, "HOME": str(tmp_path)})
    assert result.returncode == 1 and "no FieldStation42 folder" in result.stderr


def test_desktop_entry_template():
    text = (LINUX / "fieldstation42.desktop").read_text()
    assert "Exec=@PREFIX@/FieldStation42" in text
    assert "Icon=fieldstation42" in text
    assert "Categories=AudioVideo;" in text


def test_make_installer_header_and_extract(tmp_path):
    make_installer = _load(LINUX / "make_installer.py", "make_installer")
    dist = tmp_path / "dist" / "FieldStation42"
    dist.mkdir(parents=True)
    (dist / "FieldStation42").write_text("#!/bin/sh\necho fake\n")
    (dist / "_internal").mkdir()
    (dist / "_internal" / "real").write_text("x")
    (dist / "_internal" / "link").symlink_to("real")

    out = make_installer.build(dist, "9.9.9", tmp_path / "out" / "installer.run")
    head = out.read_bytes()[:4096]
    assert head.startswith(b"#!/bin/sh")
    assert b"FieldStation42 9.9.9" in head and b"__ARCHIVE_BELOW__" in head
    assert os.access(out, os.X_OK)

    if shutil.which("sh") and shutil.which("tar"):
        subprocess.run(["sh", str(out), "--check"], check=True)
        target = tmp_path / "x"
        subprocess.run(["sh", str(out), "--extract-only", str(target)], check=True)
        assert (target / "FieldStation42" / "FieldStation42").exists()
        assert (target / "install.sh").exists() and (target / "steam_shortcut.py").exists()
        assert (target / "FieldStation42" / "_internal" / "link").is_symlink()


def test_windows_installer_script_shape():
    text = (ROOT / "packaging" / "windows" / "FieldStation42.iss").read_text()
    assert "PrivilegesRequired=lowest" in text
    assert 'Source: "{#SourceDir}\\*"' in text
    assert "netsh.exe" in text and "localport=4242" in text
    assert "usPostUninstall" in text  # asks before deleting the data folder
    assert (ROOT / "packaging" / "windows" / "after-install.txt").exists()


# ------------------------------------------------------- graphical wizard

@pytest.fixture
def fake_payload(tmp_path, monkeypatch):
    """A release folder with a stand-in executable and the real scripts."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    payload = tmp_path / "payload"
    (payload / "FieldStation42").mkdir(parents=True)
    exe = payload / "FieldStation42" / "FieldStation42"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    for name in ("install.sh", "uninstall.sh", "steam_shortcut.py", "fieldstation42.desktop", "fieldstation42.png"):
        shutil.copy2(LINUX / name, payload / name)
    return payload, home


def _pump_until(app, predicate, timeout=60):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None, reason="Linux installer")
def test_wizard_installs_then_uninstalls(fake_payload, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from fs42.app import setup_wizard

    payload, home = fake_payload
    app = QApplication.instance() or QApplication([])
    prefix = setup_wizard._default_prefix()
    assert prefix == home / ".local/share/fieldstation42/app"

    wizard = setup_wizard._build_wizard("install", payload, prefix)()
    wizard.show()
    assert wizard.currentId() == 0
    wizard.next()
    options = wizard.page(1)
    options.autostart.setChecked(True)
    options.gamepad.setChecked(True)
    options.steam.setChecked(False)
    wizard.next()  # commit: runs install.sh
    assert _pump_until(app, lambda: wizard.page(2).done)
    assert wizard.page(2).ok, wizard.page(2).log.toPlainText()
    assert (prefix / "FieldStation42").exists()
    assert (home / ".local/share/applications/fieldstation42.desktop").exists()
    assert (home / ".config/autostart/fieldstation42.desktop").exists()
    assert json.loads((home / ".local/share/fieldstation42/confs/main_config.json").read_text())["gamepad"] is True
    wizard.next()
    wizard.page(3).launch.setChecked(False)
    wizard.accept()

    remover = setup_wizard._build_wizard("uninstall", None, prefix)()
    remover.show()
    remover.next()
    remover.page(1).purge.setChecked(True)
    remover.next()
    assert _pump_until(app, lambda: remover.page(2).done)
    assert remover.page(2).ok, remover.page(2).log.toPlainText()
    assert not (home / ".local/share/fieldstation42").exists()
    assert not (home / ".local/share/applications/fieldstation42.desktop").exists()
    assert not (home / ".config/autostart/fieldstation42.desktop").exists()


def test_wizard_payload_and_prefix_detection(tmp_path, monkeypatch):
    from fs42.app import setup_wizard

    assert setup_wizard._find_payload(str(tmp_path)) is None
    (tmp_path / "FieldStation42").mkdir()
    (tmp_path / "FieldStation42" / "FieldStation42").write_text("")
    (tmp_path / "install.sh").write_text("")
    assert setup_wizard._find_payload(str(tmp_path)) == tmp_path

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert setup_wizard._find_installed_prefix() is None
    prefix = tmp_path / "xdg" / "fieldstation42" / "app"
    prefix.mkdir(parents=True)
    (prefix / "uninstall.sh").write_text("")
    assert setup_wizard._find_installed_prefix() == prefix


def test_cli_routes_install_and_uninstall_to_the_wizard():
    from fs42.app import cli

    assert cli._SUBCOMMANDS["install"] == cli.ROLE_INSTALLER
    assert cli._SUBCOMMANDS["uninstall"] == cli.ROLE_INSTALLER
    role, command, ours, theirs = cli._split_argv(["install", "--payload", "/x", "--auto"])
    assert (role, command, theirs) == (None, "install", ["--payload", "/x", "--auto"])


def test_run_header_prefers_gui_then_falls_back():
    make_installer = _load(LINUX / "make_installer.py", "make_installer")
    header = make_installer.HEADER
    assert 'install --payload "$tmp"' in header
    assert 'sh "$tmp/install.sh" "$@"' in header
    assert "--no-gui" in header
