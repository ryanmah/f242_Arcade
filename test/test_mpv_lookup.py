import os

from fs42 import paths


def _script(tmp_path, text):
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    script = folder / "mpv"
    script.write_text(text)
    script.chmod(0o755)
    return folder


def test_flatpak_wrapper_is_skipped_when_flatpak_is_missing(tmp_path, monkeypatch):
    folder = _script(tmp_path, "#!/bin/sh\nexec flatpak run io.mpv.Mpv \"$@\"\n")
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setattr(paths, "bin_dir", lambda: tmp_path / "nothing")
    monkeypatch.setattr(paths, "_repo_root", lambda: tmp_path / "norepo")
    assert paths.bin_path("mpv") is None


def test_flatpak_wrapper_is_used_when_flatpak_exists(tmp_path, monkeypatch):
    folder = _script(tmp_path, "#!/bin/sh\nexec flatpak run io.mpv.Mpv \"$@\"\n")
    (folder / "flatpak").write_text("#!/bin/sh\n")
    (folder / "flatpak").chmod(0o755)
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setattr(paths, "bin_dir", lambda: tmp_path / "nothing")
    monkeypatch.setattr(paths, "_repo_root", lambda: tmp_path / "norepo")
    assert paths.bin_path("mpv") == folder / "mpv"


def test_source_checkout_finds_fetched_mpv(tmp_path, monkeypatch):
    vendor = tmp_path / "repo" / "packaging" / "vendor" / paths.platform_tag()
    vendor.mkdir(parents=True)
    (vendor / ("mpv.exe" if os.name == "nt" else "mpv")).write_text("x")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(paths, "bin_dir", lambda: tmp_path / "nothing")
    monkeypatch.setattr(paths, "_repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(paths, "IS_FROZEN", False)
    assert paths.bin_path("mpv").parent == vendor
