import io
import json
import urllib.error

import pytest

from fs42 import updater


def test_versions_compare_numerically():
    assert updater.parse_version("v1.0.10") == (1, 0, 10)
    assert updater.is_newer("1.0.10", "1.0.9")
    assert updater.is_newer("v1.1", "1.0.20")
    assert not updater.is_newer("1.0.20", "1.0.20")
    assert not updater.is_newer("", "1.0.0")
    assert updater.parse_version("1.2.3-beta") == (1, 2, 3)


class _Response(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _release(tag, names):
    return {
        "tag_name": tag,
        "html_url": "https://github.com/x/y/releases/tag/" + tag,
        "body": "notes",
        "assets": [{"name": n, "browser_download_url": "https://dl/" + n, "size": 10} for n in names],
    }


@pytest.fixture
def frozen_linux(monkeypatch):
    monkeypatch.setattr(updater, "mode", lambda: "release")
    monkeypatch.setattr(updater, "platform_asset_suffix", lambda: "-linux-x64-installer.run")
    monkeypatch.setattr(updater, "current_version", lambda: "1.0.20")
    monkeypatch.setattr(updater, "repo", lambda: "x/y")


def test_newer_release_with_our_installer_is_offered(frozen_linux, monkeypatch):
    body = _release("v1.0.21", ["FieldStation42-1.0.21-windows-x64-setup.exe",
                                "FieldStation42-1.0.21-linux-x64-installer.run", "SHA256SUMS"])
    monkeypatch.setattr(updater, "_get", lambda url, accept=None: _Response(json.dumps(body).encode()))
    info = updater.check()
    assert info["ok"] and info["available"]
    assert info["latest"] == "1.0.21"
    assert info["asset"]["name"] == "FieldStation42-1.0.21-linux-x64-installer.run"
    assert info["plan"]["sums_url"] == "https://dl/SHA256SUMS"


def test_same_version_is_up_to_date(frozen_linux, monkeypatch):
    body = _release("v1.0.20", ["FieldStation42-1.0.20-linux-x64-installer.run"])
    monkeypatch.setattr(updater, "_get", lambda url, accept=None: _Response(json.dumps(body).encode()))
    info = updater.check()
    assert info["ok"] and not info["available"]


def test_release_without_our_platform_is_not_offered(frozen_linux, monkeypatch):
    body = _release("v1.0.21", ["FieldStation42-1.0.21-windows-x64-setup.exe"])
    monkeypatch.setattr(updater, "_get", lambda url, accept=None: _Response(json.dumps(body).encode()))
    info = updater.check()
    assert not info["available"]
    assert "no installer for this platform" in info["error"]


def test_no_releases_yet_explains_itself(frozen_linux, monkeypatch):
    def missing(url, accept=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(updater, "_get", missing)
    info = updater.check()
    assert not info["ok"]
    assert "No release is published" in info["error"]


def _serve(monkeypatch, files):
    def fake_get(url, accept=None):
        return _Response(files[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(updater, "_get", fake_get)


def _assets(files):
    return [{"name": n, "browser_download_url": "https://dl/" + n, "size": len(b)} for n, b in files.items()]


def _run_job(info):
    job = updater.UpdateJob()
    job.start(info)
    job._thread.join(5)
    return job.status()


@pytest.fixture
def sandbox(frozen_linux, monkeypatch, tmp_path):
    monkeypatch.setattr(updater.paths, "cache", lambda *p: tmp_path.joinpath(*p))
    launched = []
    monkeypatch.setattr(updater, "launch_installer", lambda path: launched.append(path))
    return launched


def test_download_is_checked_against_sha256sums(sandbox, monkeypatch):
    import hashlib

    payload = b"installer bytes"
    name = "FieldStation42-1.0.21-linux-x64-installer.run"
    files = {name: payload, "SHA256SUMS": f"{hashlib.sha256(payload).hexdigest()}  {name}\n".encode()}
    _serve(monkeypatch, files)
    info = {"ok": True, "mode": "release", "available": True, "latest": "1.0.21", "plan": updater.pick_assets(_assets(files))}
    status = _run_job(info)
    assert status["state"] == "installing", status
    assert sandbox and sandbox[0].read_bytes() == payload

    # A corrupted download is refused and never launched.
    sandbox.clear()
    files[name] = b"tampered!!!!!!!"
    info["plan"]["files"][0]["size"] = len(files[name])
    import shutil
    shutil.rmtree(updater.paths.cache("updates"), ignore_errors=True)
    status = _run_job(info)
    assert status["state"] == "error" and "checksum" in status["error"]
    assert not sandbox


def test_steamos_parts_are_joined_and_checked(sandbox, monkeypatch):
    import hashlib

    whole = bytes(range(256)) * 50
    files = {f"FieldStation42-steamos.part{i:02d}": whole[i * 5000:(i + 1) * 5000] for i in range(3)}
    files["FieldStation42-steamos.sha256"] = (hashlib.sha256(whole).hexdigest() + "\n").encode()
    files["FieldStation42-1.0.19-steamos.part00"] = b"old leftovers are ignored"
    files["FieldStation42-setup.exe"] = b"windows"
    plan = updater.pick_assets(_assets(files))
    assert [f["name"] for f in plan["files"]] == [f"FieldStation42-steamos.part{i:02d}" for i in range(3)]
    _serve(monkeypatch, files)
    status = _run_job({"ok": True, "mode": "release", "available": True, "latest": "1.0.21", "plan": plan})
    assert status["state"] == "installing", status
    assert sandbox[0].name == "FieldStation42-steamos-installer.run"
    assert sandbox[0].read_bytes() == whole


def test_a_missing_part_means_no_installer(frozen_linux):
    files = {"FieldStation42-steamos.part00": b"a", "FieldStation42-steamos.part02": b"c"}
    assert updater.pick_assets(_assets(files)) is None


def test_windows_split_setup_is_recognised(monkeypatch):
    monkeypatch.setattr(updater.os, "name", "nt")
    monkeypatch.setattr(updater, "platform_asset_suffix", lambda: "-windows-x64-setup.exe")
    files = {"FieldStation42-setup.exe": b"x", "FieldStation42-setup-1.bin": b"1",
             "FieldStation42-setup-2.bin": b"2", "FieldStation42-steamos.part00": b"linux"}
    plan = updater.pick_assets(_assets(files))
    assert plan["launch"] == "FieldStation42-setup.exe"
    assert [f["name"] for f in plan["files"]] == ["FieldStation42-setup.exe", "FieldStation42-setup-1.bin", "FieldStation42-setup-2.bin"]
    # The single-file setup CI publishes wins when both are there.
    files["FieldStation42-1.0.21-windows-x64-setup.exe"] = b"one"
    assert updater.pick_assets(_assets(files))["launch"] == "FieldStation42-1.0.21-windows-x64-setup.exe"
