# FieldStation42 (desktop)

FieldStation42 turns your computer into a broadcast and cable TV simulator.
Instead of picking something to watch, you flip channels. Scheduled programming
plays on its own timeline, with commercials, station bumps, and all the
in-between stuff that made old-school TV feel alive.

This is a fork of [shane-mason/FieldStation42](https://github.com/shane-mason/FieldStation42)
that ships **everything as one executable for Windows and Linux** — the player,
the on-screen display, the settings console and the phone remote, with mpv
bundled in. Nothing to install, no Python, no shell scripts.

![A cable box next to a TV](docs/cable_cover_3.png?raw=true)

## Get it running

**Windows:** download `FieldStation42-<version>-windows-x64-setup.exe` and run
it. It installs for your user (no administrator needed), adds Start Menu and
optional desktop/startup shortcuts, and offers to open port 4242 in Windows
Firewall so the phone remote works. Windows shows a SmartScreen warning for
unsigned downloads: **More info → Run anyway**.

**Linux and Steam Deck:** download `FieldStation42-<version>-linux-x64-installer.run`
and run it. A setup wizard opens — install location, start at login, gamepad
control, add to Steam — and does the rest. To run it, either:

- in your file manager, right-click the file → *Properties* → *Permissions* →
  tick *Is executable*, then double-click it (on the Deck's Desktop Mode that's
  Dolphin), or
- from a terminal: `sh FieldStation42-*-linux-x64-installer.run`

It installs into your home folder (`~/.local/share/fieldstation42/app` — nothing
needs root, and SteamOS's read-only system is left alone) and adds an
application menu entry. On a Steam Deck (or Bazzite / ChimeraOS) it also turns
on gamepad input and offers to add FieldStation42 to your Steam library so you
can launch it from Gaming Mode. Steam has to be closed for that last step; the
installer does it for you and reopens Steam afterwards.

The wizard is the application's own Qt, so it needs nothing from your system
either. Without a display it becomes a terminal installer with the same
questions; `--yes` takes every default without asking, `--help` lists the
options.

Then:

1. Start FieldStation42. It plays static until you add a channel.
2. Press **Escape** in the video window (or open `http://localhost:4242`) and
   add a channel by pointing it at a folder of video files.
3. Rebuild the catalog and add a week of schedule — both are in the menu.

The first launch creates a data folder for your configs, catalog and
schedules — `%LOCALAPPDATA%\FieldStation42` on Windows,
`~/.local/share/fieldstation42` on Linux — and seeds it with example configs
and the default sign-off, static and off-air footage. Your video files stay
wherever they already are.

### Which download?

| You want | File |
|---|---|
| Install it (Windows) | `FieldStation42-<version>-windows-x64-setup.exe` |
| Install it (Linux, Steam Deck) | `FieldStation42-<version>-linux-x64-installer.run` |
| A single file to try, nothing installed | `FieldStation42-<version>-windows-x64.exe` / `...-linux-x64` |
| A folder to unpack yourself | the `.zip` / `.tar.gz` folder build |
| Web-source channels | additionally the `webengine` add-on |
| To see errors on Windows | `-debug.exe`, which keeps a console open |

Everything is included — mpv, ffmpeg, Python, Qt. There is nothing else to
install on either platform. The single-file build unpacks itself on every
launch, which costs a few seconds of startup; the installed and folder builds
start instantly.

### Uninstalling

Windows: *Apps → Installed apps → FieldStation42*, or the Start Menu entry. The
uninstaller asks whether to keep your data folder.

Linux: right-click the FieldStation42 menu entry → *Uninstall*, or run
`~/.local/share/fieldstation42/app/FieldStation42 uninstall` for the wizard,
or `~/.local/share/fieldstation42/app/uninstall.sh` (add `--purge` to delete
the data folder as well) from a terminal. All of them also remove the Steam
library entry.

## Commands

```
FieldStation42                  # the TV and the web console
FieldStation42 server           # web console only
FieldStation42 tui              # terminal admin interface
FieldStation42 build --help     # catalog and schedule tools
FieldStation42 doctor           # check this machine's setup
FieldStation42 uninstall        # Linux: graphical uninstaller
FieldStation42 --portable       # keep all data beside the executable
```

Press `q` in the video window, or close it, to quit everything.

## The on-screen menu

Press **Escape** while watching (or **MENU** on the phone remote, or Start on a
gamepad) to manage channels without leaving the TV:

- **Stations** — see what's configured, what's playing, how much is scheduled
- **Add a station** — pick a folder of media; subfolders become the shows,
  a `bumps` or `commercials` subfolder gives you real breaks on the half hour,
  and a folder of loose files becomes a looping channel. Name it, give it a
  number, and it builds its catalog and a week of schedule right there.
- **Per station** — tune to it, hide it from the channel list, rebuild its
  catalog, extend or reset its schedule, delete it
- **Rebuild all catalogs** / **Add a week to all schedules**

Arrow keys move, Enter selects, Backspace/Escape go back. The keys work
whether the video window or the menu has focus. The phone remote gets a D-pad
for the same thing, and mirrors what the menu is showing.

Anything the menu doesn't cover is still in the web console, whose address the
menu shows on its first page.

If the menu ever fails to appear on top of the video (a window-manager quirk),
set `"menu_drop_fullscreen": true` in `main_config.json` and mpv will step out
of fullscreen while the menu is open.

If something isn't working, run `FieldStation42 doctor`. It reports where your
data lives, which helper binaries it found, whether the guide channel and
overlays can start, and whether mpv actually accepts a connection.

## What it does

- **Scheduled programming** — daily lineups, prime time blocks, marathons, seasonal programming
- **Multiple channel types** — network TV, movie channels, guide channels, radio stations, IPTV streams, looping displays
- **Commercials and bumps** — short clips fill the breaks, just like real TV
- **Web console** — configure and manage your stations from a browser
- **Web remote** — change channels and control playback from your phone
- **On-screen display** — channel banners, station logos and a volume meter over the video
- **REST API** — control playback, change channels, query status, build your own integrations

## Running from source

```bash
git clone https://github.com/ryanmah/f242_Arcade
cd f242_Arcade
python3 -m venv env && source env/bin/activate
pip install -r install/requirements.txt
python fieldstation42.py
```

From a checkout the data directory is the repo root, exactly as upstream
behaves, so existing installs and configs work unchanged. You will need `mpv`
and `ffmpeg` on your PATH (`sudo apt install mpv ffmpeg`), or point
`mpv_path` / `ffprobe_path` in `confs/main_config.json` at them.

Building the distributable and the installers:

```bash
python packaging/fetch_binaries.py          # downloads the pinned mpv and ffmpeg (needs 7-Zip for the Windows target)
pip install -r install/requirements-build.in
pyinstaller --clean --noconfirm packaging/fieldstation42.spec -- --mode onedir
python packaging/smoke_test.py dist/FieldStation42/FieldStation42

# Linux / SteamOS self-extracting installer
python packaging/linux/make_installer.py --dist dist/FieldStation42

# Windows installer (Inno Setup 6 on PATH)
iscc /DAppVersion=1.0.0 packaging\windows\FieldStation42.iss
```

`packaging/binaries.lock.json` pins which mpv and ffmpeg builds go in;
`python packaging/fetch_binaries.py --update` moves every pin to the newest
upstream release. CI (`.github/workflows/build.yml`) builds all of this for
both platforms on every push and attaches it to releases on `v*` tags.

## Configuration

Station configs are JSON files in your data folder's `confs/`. Nine annotated
examples are seeded into `confs/examples/`. `confs/main_config.json` holds
application settings; this fork adds a few:

| Key | Default | What it does |
|---|---|---|
| `mpv_path` | bundled, then PATH | Use a specific mpv build |
| `ffprobe_path` | bundled, then PATH | Use a specific ffprobe |
| `osd_backend` | `auto` | `mpv` or `none` |
| `legacy_socket_files` | on for Linux | Mirror player status to `runtime/play_status.socket` for existing scripts |
| `menu_drop_fullscreen` | `false` | Take mpv out of fullscreen while the on-screen menu is open |
| `gamepad` | `false` | Poll a controller (XInput on Windows, `/dev/input/js*` on Linux) for menu navigation and channel changes |
| `volume_step` | `5` | Percent per volume up/down press |

Upstream's documentation at [fieldstation42.com](https://fieldstation42.com)
covers channel configuration, scheduling and the API, and applies here too.

## Differences from upstream

See [DIVERGENCE.md](DIVERGENCE.md). The short version: the on-screen display
draws through mpv instead of a transparent OpenGL window (which cannot
composite over fullscreen video on Windows), volume goes through mpv instead of
`amixer`/`pactl`, inter-process state lives in SQLite instead of unlocked
files, and the Raspberry Pi hardware support is gone because `evdev` has no
Windows build.

That document also lists the upstream bugs found while porting, several of
which are worth fixing upstream too.

## Licence and bundled software

FieldStation42 is MPL-2.0 (see [LICENSE](LICENSE)). The builds bundle mpv
(GPLv2+), ffmpeg (GPLv3) and Qt (LGPLv3) as separate programs — see
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for the notices and the
corresponding source offer.

## Support the original project

FieldStation42 is [Shane Mason's](https://github.com/shane-mason/FieldStation42)
work; this fork only changes how it is packaged. If you get value from it,
consider supporting it on [Patreon](https://www.patreon.com/cw/FieldStation42).
