# How this fork differs from upstream

Fork of [shane-mason/FieldStation42](https://github.com/shane-mason/FieldStation42),
branched from `143f0bf` (which is upstream `3e13bc8` plus seven local commits).
Goal: one executable, containing the player, on-screen display, web console and
phone remote, that runs on both Windows and Linux with nothing else to install.

Keeping future upstream merges cheap is an explicit design constraint. New
behaviour lives in new files that upstream will never touch; edits to existing
upstream files are one-line substitutions wherever possible.

## Added

| File | Why |
|---|---|
| `fieldstation42.py` | Single entry point. Roles are selected by a hidden `--fs42-role` flag or a subcommand. |
| `fs42/app/` | CLI dispatch, process supervisor, role table, `doctor` self-check. |
| `fs42/paths.py` | Separates bundled read-only resources from writable user data. Upstream resolved everything against the working directory, which cannot work for a frozen binary. |
| `fs42/ipc.py` | SQLite WAL state bus replacing the `runtime/*.socket` files and the `shelve`. |
| `fs42/platform_compat.py` | Process liveness, console-free subprocesses, mpv IPC naming, file locking, atomic writes. |
| `fs42/ffmpeg_tools.py` | Direct ffprobe/ffmpeg calls, replacing `ffmpeg-python`. |
| `fs42/osd/backends/` | On-screen display rendered by mpv. |
| `fs42/osd/config.py`, `fs42/osd/logo_selector.py` | The configuration models and logo-selection logic, extracted from the GLFW implementation. |
| `fs42/fs42_server/overlay_static.py` | Serves bundled and user-supplied web assets from one mount. |
| `fs42/menu/` | The on-screen channel menu (Escape): a Qt window in its own process, driven by keyboard, the phone remote's D-pad, or a gamepad, all through the state bus. |
| `fs42/build_jobs.py` | Catalog/schedule jobs shared by the web console's build endpoints and the menu. |
| `packaging/menu_e2e.py` | Drives the menu over HTTP against a live player and checks what the player did. |
| `packaging/` | PyInstaller spec, runtime hook, binary fetcher, smoke tests. |
| `packaging/windows/FieldStation42.iss` | Inno Setup script: per-user installer, Start Menu entries, optional autostart and firewall rule, data-preserving uninstall. |
| `fs42/app/setup_wizard.py` | Qt install/uninstall wizard (`FieldStation42 install` / `uninstall`) that the `.run` starts from its own payload, so the Linux installer has a GUI without depending on anything from the host; it drives `install.sh` / `uninstall.sh` underneath. |
| `packaging/linux/` | Self-extracting `.run` installer for Linux and SteamOS: home-directory install, `.desktop` entry, autostart, gamepad-on and Steam library entry on a Deck (`steam_shortcut.py` edits `shortcuts.vdf`), uninstaller. |

## Removed

| Path | Reason |
|---|---|
| `fs42/pi/` | Raspberry Pi cable box and IR remote controller. `evdev` has no Windows build at all, so its presence in requirements made `pip install` fail outright on Windows. |
| `fs42/pico/` | RP2040 CircuitPython firmware; not host code. |
| `fs42/command_input.py`, `fs42/hot_start.sh` | Opened `/dev/ttyAMA0` at import time. |
| `fs42/remote/` | A second FastAPI app duplicating the web remote already served at `/remote`. Nothing imported it. |
| `fs42/diagchannel/` | Imported by nothing. |
| `fs42/osd/render.py`, `fs42/osd/logo_display.py` | The GLFW/OpenGL renderer, replaced by mpv overlays. |
| `install/systemd/fs42-cable-box.service.template`, `fs42-remote-controller.service.template` | Follow the Pi support. |

Resolving a merge conflict in any of these trees means `git rm`.

## Changed behaviour

**On-screen display.** Upstream floated a transparent GLFW window over mpv.
That window is created as *exclusive fullscreen* GL, and `GLFW_TRANSPARENT_FRAMEBUFFER`
is only honoured for windowed-mode windows with a compositor — so on Windows
the OSD and mpv fight for the display mode and one of them loses. The OSD now
draws through mpv's own `osd-overlay` (ASS text) and `overlay-add` (BGRA
bitmaps for logos). `osd/osd.json` is unchanged field for field.

**Volume.** Upstream shelled out to `amixer`, `pactl` or `wpctl` and returned
HTTP 500 when none existed, which is every Windows machine. Volume now goes
through mpv's own `volume` and `mute` properties. The HTTP endpoints are
unchanged, so the web remote needed no edit. As a side effect volume now
scopes to FieldStation42 rather than the system master, which is what a TV
appliance should do.

**Inter-process state.** The `runtime/*.socket` files were plain files, read
and truncated without locking. On Windows a writer's `open(path, "w")` raises
`PermissionError` while a reader holds the file. State now lives in
`runtime/fs42_state.db` (SQLite, WAL). Status is still mirrored to
`runtime/play_status.socket` — atomically, which also fixes upstream's torn
reads — so existing user scripts keep working. Set `legacy_socket_files: false`
in `main_config.json` to turn the mirror off.

**Player commands from the console.** Every API endpoint that talks to the
player (`reload_data` after a catalog build, PPV `play_file`, PPV web keys,
guide, ticker, stop, mpv commands, volume) now goes through one
`send_player_command` helper that uses the in-process queue when it exists and
the state bus otherwise. Under upstream, several of these silently did nothing
when the API ran as a separate process.

**Station edits reach the running player.** Upstream's `StationManager` is a
per-process singleton; a station added in the web console (a separate
process) was invisible to the player until restart, and deleting the station
currently playing crashed `play_slot`. There is now a `reload_stations`
player command that re-reads the files, keeps the current channel if it still
exists and retunes if not; the API and menu call `reload_if_changed()` before
listing stations so they never show a stale list. The menu also verifies every
config it writes by running it through the same load path first, because
`write_station_config` calls `exit(-1)` on a bad file *after* writing it.

**Process model.** `multiprocessing` uses the `spawn` start method on every
platform, not just Windows, so there is one behaviour to test rather than two.

**Paths.** From a source checkout, the data directory is still the repo root,
so nothing changes. In the packaged build it is `%LOCALAPPDATA%\FieldStation42`
or `~/.local/share/fieldstation42`. `FS42_HOME` overrides it; `--portable`
keeps everything beside the executable. Relative paths in station configs
(`catalog/nbc_catalog`) resolve against that root, so existing configs work
unmodified.

## Bugs fixed in passing

These are upstream bugs found while porting, worth reporting back:

- `fs42/overlay/ticker.py` and `now_playing.py` passed **closures** as
  `multiprocessing.Process` targets. Closures cannot be pickled, so both
  crash under the `spawn` start method (i.e. always, on Windows).
- Both files probed process liveness with `os.kill(pid, 0)`. On Windows
  `os.kill` maps to `TerminateProcess` for every signal except `CTRL_C_EVENT`
  and `CTRL_BREAK_EVENT` — so the probe *kills* whatever owns that pid.
- `fs42/fs42_server/api/media.py`'s `safe_resolve` checked containment with
  `str.startswith`, so a path under `/opt/fs42-evil` passed a check rooted at
  `/opt/fs42`. It also stripped leading slashes but not a Windows drive
  letter, which `os.path.join` then treats as absolute.
- `fs42/osd/main.py`'s `HybridDisplay` branch appended `logo` and `osd` —
  names bound by *previous* loop iterations — instead of the `status_logo`
  and `status_osd` it had just built.
- `fs42/station_player.py` imported `fs42.guide_tk` (and therefore tkinter)
  unconditionally at module scope, so a missing tk stopped playback entirely
  even with no guide channel configured.
- `fs42/fs42_server/api/build.py` and `ppv.py` wrote straight to the
  in-process command queue, so post-build reloads, PPV playback and PPV web
  keys silently did nothing when the API ran as a separate process.
- `StationManager.delete_station_config` removed the config file but left the
  station's catalog rows and schedule blocks in the database.
- `fs42/guide_builder.py` took `template_dir` and `static_dir` arguments
  pointing at `fs42/guide_render/templates/`, a directory that does not exist
  in the repository. Neither was ever read.
- `install/requirements.txt` was out of date with `requirements.in`, missing
  `jsonschema`, `requests` and `mutagen` — all imported by the code. CI
  installed from the `.txt`, so it tested a different dependency set than
  users got.
