#!/usr/bin/env python3
"""End-to-end checks against a built FieldStation42 executable.

Runs in CI on both platforms.  Everything happens under a throwaway
``FS42_HOME`` so it never touches a real installation.

    python packaging/smoke_test.py dist/FieldStation42/FieldStation42
    python packaging/smoke_test.py --source          # test the checkout instead

The catalog-rebuild case is the highest-value one: it exercises ffprobe,
moviepy and sqlite together, which is where a frozen build usually first
falls over.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"

PASSED = []
FAILED = []


class Runner:
    def __init__(self, command, home):
        self.command = command
        self.home = Path(home)

    def env(self, **extra):
        environment = dict(os.environ)
        environment["FS42_HOME"] = str(self.home)
        environment["PYTHONUNBUFFERED"] = "1"
        environment.update(extra)
        return environment

    def run(self, *args, timeout=300):
        return subprocess.run(
            self.command + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self.env(),
            cwd=str(ROOT),
        )

    def popen(self, *args):
        return subprocess.Popen(
            self.command + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self.env(),
            cwd=str(ROOT),
        )


def http_status(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return None, b""


def wait_for_server(port, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, _ = http_status(f"http://127.0.0.1:{port}/player/status", timeout=2)
        if status == 200:
            return True
        time.sleep(1)
    return False


# ---------------------------------------------------------------- the checks


def test_version(runner):
    result = runner.run("--version", timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FieldStation42" in result.stdout, result.stdout
    return "reports version and layout"


def test_cli_help(runner):
    """Proves the whole station_42 import graph loads under the build."""
    result = runner.run("build", "--help", timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rebuild_catalog" in result.stdout, result.stdout
    return "build tooling imports and runs"


def test_first_run_seeding(runner):
    result = runner.run("build", "-e", timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr

    expected = [
        runner.home / "confs" / "main_config.json",
        runner.home / "confs" / "examples",
        runner.home / "osd" / "osd.json",
        runner.home / "runtime",
        runner.home / "catalog",
    ]
    missing = [str(p) for p in expected if not p.exists()]
    assert not missing, f"not seeded: {missing}"

    examples = list((runner.home / "confs" / "examples").glob("*.json"))
    assert examples, "no example station configs were seeded"
    return f"seeded data directory with {len(examples)} example configs"


def test_catalog_build(runner):
    """ffprobe + moviepy + sqlite, all under the build."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "SKIP: ffmpeg not on PATH, cannot generate test media"

    content = runner.home / "catalog" / "smoke" / "feature"
    content.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        target = content / f"clip_{index}.mp4"
        if target.exists():
            continue
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=4",
             "-pix_fmt", "yuv420p", str(target)],
            check=True,
        )

    station = {
        "network_name": "SMOKE",
        "channel_number": 3,
        "network_type": "standard",
        "content_dir": "catalog/smoke",
        "commercial_dir": "catalog/smoke",
        "bump_dir": "catalog/smoke",
        "runtime_dir": "catalog/smoke",
        "schedule_increment": 30,
        "break_duration": 10,
        "commercial_free": True,
    }
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        station[day] = {str(hour): {"tags": ["feature"]} for hour in range(24)}
    (runner.home / "confs" / "smoke.json").write_text(json.dumps({"station_conf": station}))

    result = runner.run("build", "--rebuild_catalog", timeout=600)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]

    database = runner.home / "runtime" / "fs42_fluid.db"
    assert database.exists() and database.stat().st_size > 0, "catalog database is empty"
    return "rebuilt a catalog from real media"


def test_doctor(runner):
    """The build's own self-check: binaries, Qt/tk, and a live mpv IPC round-trip."""
    result = runner.run("doctor", timeout=300)
    output = result.stdout + result.stderr
    assert "Everything checks out" in output or "problem(s) found" in output, output[-2000:]

    # mpv IPC is not optional - the player is built on it.
    assert "mpv IPC failed" not in output, output[-2000:]
    assert result.returncode == 0, f"doctor reported problems:\n{output[-2000:]}"
    return "self-check passed, including a live mpv IPC round-trip"


def test_api(runner):
    port = 4242
    process = runner.popen("server")
    try:
        assert wait_for_server(port), "API server did not come up"

        for path in (
            "/",
            "/static/themes/default.css",
            "/about/themes",
            "/player/status",
            "/player/info",
            "/docs",
        ):
            status, _ = http_status(f"http://127.0.0.1:{port}{path}")
            assert status == 200, f"{path} returned {status}"

        # Path sandbox: both of these must be refused, on both platforms.
        for hostile in ("../../etc/passwd", "..%5C..%5CWindows%5Cwin.ini", "C:%5CWindows%5Cwin.ini"):
            status, _ = http_status(f"http://127.0.0.1:{port}/media/file?path={hostile}")
            assert status in (400, 404), f"path sandbox let {hostile} through with {status}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            raise AssertionError("API server did not exit within 15s")
    return "served the console and refused traversal paths"


def test_single_extraction_dir(runner):
    """One-file builds must not re-extract per child process."""
    if IS_WINDOWS:
        return "SKIP: extraction-directory check is Linux-only"
    temp_root = Path(tempfile.gettempdir())
    before = {p.name for p in temp_root.glob("_MEI*")}

    process = runner.popen("server")
    try:
        if not wait_for_server(4242):
            return "SKIP: server did not start"
        time.sleep(2)
        during = {p.name for p in temp_root.glob("_MEI*")} - before
        assert len(during) <= 1, f"expected at most one extraction dir, found {sorted(during)}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
    return "reuses a single extraction directory"


CHECKS = [
    ("version", test_version),
    ("cli help", test_cli_help),
    ("first-run seeding", test_first_run_seeding),
    ("catalog build", test_catalog_build),
    ("doctor", test_doctor),
    ("api and path sandbox", test_api),
    ("extraction dir", test_single_extraction_dir),
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", nargs="?", help="Path to the built executable.")
    parser.add_argument("--source", action="store_true", help="Test the checkout instead of a build.")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary FS42_HOME.")
    args = parser.parse_args(argv)

    if args.source or not args.executable:
        command = [sys.executable, str(ROOT / "fieldstation42.py")]
        label = "source checkout"
    else:
        command = [str(Path(args.executable).resolve())]
        label = args.executable

    home = Path(tempfile.mkdtemp(prefix="fs42-smoke-"))
    runner = Runner(command, home)
    print(f"Testing {label}\nFS42_HOME={home}\n")

    for name, function in CHECKS:
        start = time.time()
        try:
            detail = function(runner)
        except Exception as e:
            FAILED.append((name, e))
            print(f"  FAIL  {name}  ({time.time() - start:.1f}s)\n        {e}")
        else:
            if isinstance(detail, str) and detail.startswith("SKIP"):
                print(f"  skip  {name}  - {detail[6:]}")
            else:
                PASSED.append(name)
                print(f"  ok    {name}  ({time.time() - start:.1f}s) - {detail}")

    if not args.keep:
        shutil.rmtree(home, ignore_errors=True)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
