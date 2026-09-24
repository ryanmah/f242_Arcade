#!/usr/bin/env python3
"""Drive the in-app menu over the HTTP API and check what the player did.

Runs against a live FieldStation42 (started separately, e.g. under Xvfb).
Every step reads the page the menu published and moves the cursor to a row
by name, so it tests what a person would see rather than a keystroke count.

    python packaging/menu_e2e.py --home /tmp/fs42e2e --folder retro
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:4242"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def post(path):
    req = urllib.request.Request(BASE + path, method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def page():
    return get("/player/menu").get("page") or {}


def wait_page(predicate, seconds=30, what="page"):
    deadline = time.time() + seconds
    while time.time() < deadline:
        p = page()
        if p and predicate(p):
            return p
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}; last page: {json.dumps(page())[:400]}")


def action(name, settle=0.35):
    post(f"/player/menu/input/{name}")
    time.sleep(settle)


def select_row(contains, settle=0.6):
    """Move the cursor to the first enabled row whose title contains the text, then select it."""
    p = wait_page(lambda p: any(contains in r["title"] for r in p["rows"]), what=f"row containing {contains!r}")
    rows = p["rows"]
    target = next(i for i, r in enumerate(rows) if contains in r["title"] and r["enabled"])
    cursor = p["cursor"]
    steps = target - cursor
    for _ in range(abs(steps)):
        action("down" if steps > 0 else "up", 0.15)
    p = page()
    assert p["cursor"] == target, f"cursor {p['cursor']} != {target} for {contains!r}: {[r['title'] for r in p['rows']]}"
    action("select", settle)


def stations():
    return [(s["channel_number"], s["network_name"]) for s in get("/summary/")["summary_data"]]


def status():
    d = get("/player/status")
    return d.get("channel_number"), d.get("network_name"), d.get("status")


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--folder", default="retro", help="folder under catalog/ to add as a station")
    parser.add_argument("--channel", type=int, default=9)
    args = parser.parse_args()
    home = Path(args.home)
    conf = home / "confs" / f"{args.folder}.json"

    before = stations()
    print(f"stations before: {before}")

    # ---- open
    post("/player/menu/open")
    p = wait_page(lambda p: p.get("page") == "HomePage", what="HomePage")
    check("menu opened on Home", True, p["title"])
    check("player marked menu open", get("/player/menu")["open"])

    # ---- add a station
    select_row("Stations")
    select_row("Add a station")
    select_row(f"{args.folder}/")
    select_row("Use this folder")
    p = page()
    if p.get("page") == "TagsPage":
        select_row("Continue")
    wait_page(lambda p: p.get("page") == "NamePage", what="NamePage")
    action("down")                       # channel number field
    for d in str(args.channel):
        action(f"digit_{d}", 0.15)
    select_row("Create station", settle=1.0)
    p = wait_page(lambda p: p.get("page") == "ProgressPage" and any("Done" in r["title"] or "Back" in r["title"] for r in p["rows"]),
                  seconds=180, what="build to finish")
    check("build finished", any(r["title"] == "Done" for r in p["rows"]), p.get("subtitle"))
    check("config written", conf.exists(), str(conf))
    select_row("Done")

    after = stations()
    check("API sees the new station without restart", (args.channel, args.folder.title()) in after or any(n.lower() == args.folder for _, n in after), str(after))

    # ---- tune to it from its detail page
    wait_page(lambda p: p.get("page") == "StationsPage", what="StationsPage")
    select_row(args.folder.title())
    select_row("Tune to this channel", settle=1.0)
    deadline = time.time() + 20
    while time.time() < deadline and get("/player/menu")["open"]:
        time.sleep(0.5)
    check("menu closed after tune", not get("/player/menu")["open"])
    time.sleep(6)
    check("player tuned to the new station", status()[0] == args.channel, str(status()))

    # ---- delete the station that is playing
    post("/player/menu/open")
    wait_page(lambda p: p.get("page") == "HomePage", what="HomePage")
    select_row("Stations")
    select_row(args.folder.title())
    select_row("Delete this station")
    select_row("Delete it", settle=1.5)
    check("config removed", not conf.exists())
    time.sleep(8)
    # With other stations left it retunes; if that was the only one it goes
    # back to the "no channels yet" static screen.  Either way it must not die.
    after = status()
    check("player retuned (or idled) instead of crashing",
          (after[2] == "playing" and after[0] != args.channel) or after[2] == "no_channels", str(after))
    check("player process alive", subprocess.run(["pgrep", "-f", "fs42-role=player"], capture_output=True).returncode == 0)

    # ---- close and confirm normal input still works
    wait_page(lambda p: p.get("page") == "StationsPage", what="StationsPage")
    action("back", 0.6)
    action("back", 1.5)
    check("menu closed from Home", not get("/player/menu")["open"])
    get("/player/channels/up")
    time.sleep(4)
    # With the only station gone the player idles on static; a channel change
    # then has nowhere to go, which is fine.
    check("channel change still works", status()[2] in ("playing", "no_channels"), str(status()))
    print("\nall menu checks passed")


if __name__ == "__main__":
    main()
