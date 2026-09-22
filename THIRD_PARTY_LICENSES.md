# Third-party software bundled with FieldStation42

FieldStation42 is licensed under the Mozilla Public License 2.0 (see LICENSE).
The distributable builds additionally ship the programs below. They are
separate executables that FieldStation42 talks to over IPC or invokes as
subprocesses; that is mere aggregation, so their licences apply to them and
not to FieldStation42's own code.

| Component | Licence | Upstream source |
|---|---|---|
| mpv | GPL v2 or later | https://github.com/mpv-player/mpv |
| ffmpeg / ffprobe | GPL v3 | https://ffmpeg.org/ |
| Qt (via PySide6) | LGPL v3 | https://www.qt.io/ |
| Python | PSF License | https://www.python.org/ |

The mpv and ffmpeg executables are unmodified releases of these community
build projects:

| Platform | Component | Build | Notes |
|---|---|---|---|
| Windows | mpv | [zhongfly/mpv-winbuild](https://github.com/zhongfly/mpv-winbuild) | static `mpv.exe`, built from mpv git with [shinchiro's build scripts](https://github.com/shinchiro/mpv-winbuild-cmake) |
| Windows | ffmpeg / ffprobe | [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) | `win64-gpl` static |
| Linux | mpv | [pkgforge-dev/mpv-AppImage](https://github.com/pkgforge-dev/mpv-AppImage) | the `anylinux` AppImage, unpacked into a relocatable directory; its yt-dlp downloader and self-updater hooks are removed so it never reaches the network |
| Linux | ffmpeg / ffprobe | [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) | `linux64-gpl` static |

Removing two shell hooks from the AppImage's launcher does not modify mpv
itself; the mpv binary and its libraries are byte-for-byte the upstream files.

## Where to get the source

`packaging/binaries.lock.json` pins the release URL and SHA-256 of every
binary a build starts from, and every build writes what it actually bundled -
final URL, digest and version - to `bin/<platform>/MANIFEST.json` inside the
application (`FieldStation42 doctor` prints the versions). Those URLs are the
corresponding source offer required by the GPL: each points at a published
release of an unmodified upstream build, whose complete source is available
from the projects linked above. We do not patch these binaries.

## Qt / LGPL v3 and the folder build

LGPL v3 requires that you be able to replace the Qt libraries with your own
build. The one-file executable packs everything into a single archive, which
does not allow that. Every release therefore also ships a **folder build**
(`.zip` on Windows, `.tar.gz` on Linux) in which Qt's shared libraries are
ordinary, individually replaceable files. Use that build if you need to relink
against a modified Qt.

The folder build also starts instantly, because it does not re-extract a
payload on every launch. It is the better choice for anything you run often.

## UPX

Bundled binaries are not compressed with UPX. Compression would break Qt's
plugin loading, invalidate code signatures, and make it harder to verify that
the shipped binaries match the upstream releases named above.
