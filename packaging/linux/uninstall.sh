#!/bin/sh
# Remove FieldStation42 (installed by install.sh).  Keeps your data folder
# unless you say otherwise.
#
#     ~/.local/share/fieldstation42/app/uninstall.sh
#     ... --yes            remove the application, keep the data, ask nothing
#     ... --purge          also delete the data folder (configs, catalog, schedules)

set -eu

APP=FieldStation42
prefix="$(cd "$(dirname "$0")" && pwd)"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
data_dir="$data_home/fieldstation42"
yes=0
purge=""

say()  { printf '%s\n' "$*"; }
ask() {
    q="$1"; d="$2"
    if [ "$yes" = 1 ] || [ ! -t 0 ]; then [ "$d" = y ]; return; fi
    if [ "$d" = y ]; then p="[Y/n]"; else p="[y/N]"; fi
    printf '%s %s ' "$q" "$p"; read -r a || a=""
    case "${a:-$d}" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) yes=1 ;;
        --purge) purge=1 ;;
        --keep-data) purge=0 ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 1 ;;
    esac
    shift
done

if pgrep -f "$prefix/$APP( --fs42-role=|$)" >/dev/null 2>&1; then
    say "Stopping $APP..."
    pkill -INT -f "$prefix/$APP( --fs42-role=|$)" || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "$prefix/$APP( --fs42-role=|$)" >/dev/null 2>&1 || break; sleep 1; done
    pkill -KILL -f "$prefix/$APP( --fs42-role=|$)" 2>/dev/null || true
fi

rm -f "$HOME/.local/bin/fieldstation42"
rm -f "$data_home/applications/fieldstation42.desktop"
rm -f "$config_home/autostart/fieldstation42.desktop"
rm -f "$data_home/icons/hicolor/512x512/apps/fieldstation42.png"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$data_home/applications" 2>/dev/null || true

if command -v python3 >/dev/null 2>&1 && [ -f "$prefix/steam_shortcut.py" ]; then
    if pgrep -x steam >/dev/null 2>&1; then
        say "Steam is running, so its library entry was left alone. To remove it later, close Steam and run:"
        say "  python3 $data_home/fieldstation42-steam_shortcut.py --remove"
        cp "$prefix/steam_shortcut.py" "$data_home/fieldstation42-steam_shortcut.py"
    else
        python3 "$prefix/steam_shortcut.py" --remove || true
    fi
fi

if [ -z "$purge" ]; then
    if ask "Also delete your data folder $data_dir (channel configs, catalog, schedules)? Video files stored elsewhere are never touched." n; then
        purge=1
    else
        purge=0
    fi
fi

# The app lives inside the data folder; remove it first so a kept data folder
# is just data.
cd "$HOME"
rm -rf "$prefix"
if [ "$purge" = 1 ]; then
    rm -rf "$data_dir"
    rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/fieldstation42"
    say "$APP and its data are gone."
else
    say "$APP is removed. Your data is still in $data_dir."
fi
