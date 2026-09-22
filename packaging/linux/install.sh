#!/bin/sh
# FieldStation42 installer for Linux and SteamOS.
#
# Runs from the self-extracting .run (which unpacks next to this script and
# calls it), or by hand from an unpacked release folder:
#
#     ./install.sh                  # interactive
#     ./install.sh --yes            # take every default, ask nothing
#     ./install.sh --prefix DIR     # somewhere other than ~/.local/share/fieldstation42/app
#     ./install.sh --autostart      # start when you log in
#     ./install.sh --steam          # add to Steam as a non-Steam game (default on SteamOS)
#     ./install.sh --no-steam
#     ./install.sh --steamos        # treat this machine like a Steam Deck (gamepad on, Steam entry)
#     ./install.sh --gamepad        # turn on gamepad control (implied by SteamOS)
#
# The .run prefers the graphical version of this - `FieldStation42 install` -
# whenever there is a display; it collects the same options and runs this
# script underneath.
#
# Installs into the user's home: SteamOS keeps / read-only and no other
# distribution needs root for this either.  Everything the application needs
# (mpv, ffmpeg, Qt, Python) is inside the folder being copied; the only host
# requirement is a working display.

set -eu

APP=FieldStation42
here="$(cd "$(dirname "$0")" && pwd)"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
data_dir="$data_home/fieldstation42"
prefix="$data_dir/app"
bin_dir="$HOME/.local/bin"
yes=0
autostart=""
steam=""
steamos=0
gamepad=""

say()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

ask() {
    # ask "question" default(y|n) -> returns 0 for yes
    q="$1"; d="$2"
    if [ "$yes" = 1 ] || [ ! -t 0 ]; then
        [ "$d" = y ]; return
    fi
    if [ "$d" = y ]; then p="[Y/n]"; else p="[y/N]"; fi
    printf '%s %s ' "$q" "$p"
    read -r a || a=""
    case "${a:-$d}" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) prefix="$2"; shift ;;
        --prefix=*) prefix="${1#*=}" ;;
        --yes|-y) yes=1 ;;
        --autostart) autostart=1 ;;
        --no-autostart) autostart=0 ;;
        --steam) steam=1 ;;
        --no-steam) steam=0 ;;
        --steamos) steamos=1 ;;
        --gamepad) gamepad=1 ;;
        -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

[ -x "$here/$APP/$APP" ] || die "no $APP folder next to this script"

# SteamOS proper, and the community "Deck-like" distributions that boot into
# Steam's Gaming Mode.
if [ -r /etc/os-release ] && grep -Eq '^ID=(steamos|bazzite|chimeraos|holoiso)' /etc/os-release; then
    steamos=1
fi
say "Installing $APP"
say "  application : $prefix"
say "  your data   : $data_dir"
[ "$steamos" = 1 ] && say "  SteamOS detected: gamepad support on, Steam library entry offered"
say ""

# ------------------------------------------------------- stop what's running
if pgrep -f "$prefix/$APP( --fs42-role=|$)" >/dev/null 2>&1; then
    say "Stopping the running $APP..."
    pkill -INT -f "$prefix/$APP( --fs42-role=|$)" || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        pgrep -f "$prefix/$APP( --fs42-role=|$)" >/dev/null 2>&1 || break
        sleep 1
    done
    pkill -KILL -f "$prefix/$APP( --fs42-role=|$)" 2>/dev/null || true
fi

# ------------------------------------------------------------- copy the app
mkdir -p "$(dirname "$prefix")"
staging="$prefix.new"
rm -rf "$staging"
# -a keeps the symlinks inside the bundled mpv AppDir; cp -rL would balloon it.
cp -a "$here/$APP" "$staging"
for f in LICENSE THIRD_PARTY_LICENSES.md fieldstation42.png steam_shortcut.py uninstall.sh; do
    [ -e "$here/$f" ] && cp -a "$here/$f" "$staging/"
done
chmod +x "$staging/$APP" "$staging/uninstall.sh" "$staging/steam_shortcut.py" 2>/dev/null || true
rm -rf "$prefix"
mv "$staging" "$prefix"

# --------------------------------------------------------------- launchers
mkdir -p "$bin_dir"
ln -sfn "$prefix/$APP" "$bin_dir/fieldstation42"

icon_dir="$data_home/icons/hicolor/512x512/apps"
mkdir -p "$icon_dir" "$data_home/applications"
cp "$here/fieldstation42.png" "$icon_dir/fieldstation42.png"
sed "s|@PREFIX@|$prefix|g" "$here/fieldstation42.desktop" > "$data_home/applications/fieldstation42.desktop"
chmod +x "$data_home/applications/fieldstation42.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$data_home/applications" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$data_home/icons/hicolor" 2>/dev/null || true

# ------------------------------------------------------------ sanity check
# Proves the build actually runs on this machine before we say "done".
if ! "$prefix/$APP" --version >/dev/null 2>&1; then
    warn "$APP did not start; run '$prefix/$APP doctor' to see why"
fi

# Gamepad polling for the menu and channel changes: on by default on a Deck.
main_config="$data_dir/confs/main_config.json"
mkdir -p "$data_dir/confs"
[ -s "$main_config" ] || printf '{\n}\n' > "$main_config"
[ "$steamos" = 1 ] && gamepad=1
if [ "$gamepad" = 1 ] && ! grep -q '"gamepad"' "$main_config"; then
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$main_config" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    conf = json.load(f)
conf["gamepad"] = True
with open(path, "w") as f:
    json.dump(conf, f, indent=4)
    f.write("\n")
PY
        say "Enabled gamepad input in $main_config"
    fi
fi

# --------------------------------------------------------------- autostart
if [ -z "$autostart" ]; then
    if ask "Start $APP automatically when you log in?" n; then autostart=1; else autostart=0; fi
fi
if [ "$autostart" = 1 ]; then
    mkdir -p "$config_home/autostart"
    cp "$data_home/applications/fieldstation42.desktop" "$config_home/autostart/fieldstation42.desktop"
    say "Autostart enabled ($config_home/autostart/fieldstation42.desktop)"
else
    rm -f "$config_home/autostart/fieldstation42.desktop"
fi

# ------------------------------------------------------------------- steam
add_steam_shortcut() {
    command -v python3 >/dev/null 2>&1 || { warn "python3 not found; skipping the Steam shortcut"; return; }
    restart=0
    if pgrep -x steam >/dev/null 2>&1; then
        if ask "Steam must be closed to add $APP to your library. Close it now (it will be reopened)?" y; then
            steam -shutdown >/dev/null 2>&1 || pkill -x steam || true
            for _ in $(seq 1 30); do pgrep -x steam >/dev/null 2>&1 || break; sleep 1; done
            if pgrep -x steam >/dev/null 2>&1; then
                warn "Steam is still running; skipping. Later: close Steam and run"
                warn "  python3 $prefix/steam_shortcut.py --add --exe $prefix/$APP --icon $prefix/fieldstation42.png"
                return
            fi
            restart=1
        else
            say "Skipped. Later: close Steam and run"
            say "  python3 $prefix/steam_shortcut.py --add --exe $prefix/$APP --icon $prefix/fieldstation42.png"
            return
        fi
    fi
    python3 "$prefix/steam_shortcut.py" --add --exe "$prefix/$APP" --icon "$prefix/fieldstation42.png" || \
        warn "could not add the Steam shortcut"
    if [ "$restart" = 1 ]; then
        say "Restarting Steam..."
        (nohup steam >/dev/null 2>&1 &) || true
    fi
}

if [ -z "$steam" ]; then
    if [ "$steamos" = 1 ]; then d=y; else d=n; fi
    if ask "Add $APP to your Steam library (launch it from Gaming Mode / Big Picture)?" "$d"; then steam=1; else steam=0; fi
fi
[ "$steam" = 1 ] && add_steam_shortcut

# ------------------------------------------------------------------ done
say ""
say "$APP is installed."
say ""
say "  Launch it from your application menu, or run:  $bin_dir/fieldstation42"
say "  Channels:  press Escape in the video window, or open http://localhost:4242"
say "  Check:     $prefix/$APP doctor"
say "  Remove:    $prefix/uninstall.sh"
case ":$PATH:" in *":$bin_dir:"*) ;; *) say ""; say "  ($bin_dir is not on your PATH; the menu entry works regardless)";; esac
[ "$steamos" = 1 ] && { say ""; say "  On the Deck: switch to Gaming Mode and find $APP under Non-Steam games."; }
exit 0
