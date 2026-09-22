"""Top-level command line for the single executable.

The same binary is the supervisor, the player, the API server, the OSD, the
text UI and the build tool.  Which one it becomes is decided by the hidden
``--fs42-role`` flag (set when the supervisor re-execs itself) or by a
user-facing subcommand.
"""

import argparse
import logging
import os
import sys

ROLE_FLAG = "--fs42-role"

ROLE_SUPERVISOR = "supervisor"
ROLE_PLAYER = "player"
ROLE_API = "api"
ROLE_OSD = "osd"
ROLE_TUI = "tui"
ROLE_CLI = "cli"
ROLE_DOCTOR = "doctor"
ROLE_PROBE = "probe"
ROLE_INSTALLER = "installer"

ALL_ROLES = (
    ROLE_SUPERVISOR, ROLE_PLAYER, ROLE_API, ROLE_OSD, ROLE_TUI, ROLE_CLI, ROLE_DOCTOR, ROLE_PROBE,
    ROLE_INSTALLER,
)

# Subcommand the user types -> internal role.
_SUBCOMMANDS = {
    "run": ROLE_SUPERVISOR,
    "play": ROLE_PLAYER,
    "server": ROLE_API,
    "osd": ROLE_OSD,
    "tui": ROLE_TUI,
    "build": ROLE_CLI,
    "doctor": ROLE_DOCTOR,
    "install": ROLE_INSTALLER,
    "uninstall": ROLE_INSTALLER,
}


def _prepare_environment():
    """Make bundled helper binaries and optional plugins discoverable.

    Prepending the bundled bin directory to PATH means anything that shells
    out by bare name (moviepy, and any user script) finds our copies of mpv
    and ffmpeg rather than depending on a system install.
    """
    from fs42 import paths

    bin_dir = paths.bin_dir()
    if bin_dir.is_dir():
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(str(bin_dir))
            except OSError:
                pass
        ffmpeg = paths.bin_dir() / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if ffmpeg.exists():
            os.environ.setdefault("IMAGEIO_FFMPEG_EXE", str(ffmpeg))

    # Optional add-on packages (QtWebEngine ships separately to keep the base
    # executable an order of magnitude smaller).
    plugins = paths.data("plugins")
    if plugins.is_dir():
        for entry in sorted(plugins.iterdir()):
            if entry.is_dir() and str(entry) not in sys.path:
                sys.path.insert(0, str(entry))


def _configure_logging(args):
    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            format="%(asctime)s %(levelname)s:%(name)s:%(message)s", level=level
        )
    root.setLevel(level)

    logfile = getattr(args, "logfile", None)
    if logfile:
        from fs42 import paths

        target = logfile if os.path.isabs(logfile) else str(paths.logs(logfile))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        handler = logging.FileHandler(target)
        handler.setFormatter(
            logging.Formatter("%(asctime)s:%(levelname)s:%(name)s:%(message)s")
        )
        root.addHandler(handler)


def build_parser(add_help=True, with_command=True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="FieldStation42",
        description="FieldStation42 - broadcast and cable TV simulator.",
        epilog=(
            "Run with no arguments to start the TV and the web console.\n"
            "  FieldStation42 server        web console only\n"
            "  FieldStation42 tui           terminal admin interface\n"
            "  FieldStation42 build --help  catalog and schedule tools\n"
            "  FieldStation42 doctor        check this machine's setup\n"
            "  FieldStation42 install       graphical installer (Linux; run from an unpacked release)\n"
            "  FieldStation42 uninstall     graphical uninstaller (Linux)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=add_help,
    )
    parser.add_argument("--version", action="store_true", help="Print version and layout, then exit.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Chatty logging.")
    parser.add_argument("-l", "--logfile", help="Append logs to this file.")
    parser.add_argument(
        "--portable",
        action="store_true",
        help="Keep all data next to the executable instead of in your user profile.",
    )
    parser.add_argument(
        "--no-server",
        "--no_server",
        dest="no_server",
        action="store_true",
        help="Do not start the web console / API server.",
    )
    parser.add_argument(
        "-t",
        "--transition",
        choices=["long", "short", "none"],
        help="Channel change transition effect.",
    )
    parser.add_argument(
        ROLE_FLAG,
        dest="role",
        choices=ALL_ROLES,
        default=None,
        help=argparse.SUPPRESS,
    )
    if with_command:
        parser.add_argument(
            "command",
            nargs="?",
            choices=sorted(_SUBCOMMANDS),
            help="What to run.  Defaults to the full application.",
        )
    return parser


def _split_argv(argv):
    """Separate our own arguments from the role's.

    Everything after a subcommand belongs to that role - notably ``build``,
    whose flags are the whole of the upstream station_42 command line, and
    which needs its own ``--help`` rather than ours.
    """
    role = None
    command = None
    ours = []
    theirs = []

    index = 0
    while index < len(argv):
        token = argv[index]

        if token == ROLE_FLAG:
            if index + 1 < len(argv):
                role = argv[index + 1]
                index += 2
                continue
            index += 1
            continue
        if token.startswith(ROLE_FLAG + "="):
            role = token.split("=", 1)[1]
            index += 1
            continue

        if command is None and token in _SUBCOMMANDS:
            command = token
            theirs.extend(argv[index + 1:])
            break

        ours.append(token)
        index += 1

    return role, command, ours, theirs


def dispatch(argv) -> int:
    argv = list(argv)
    role, command, ours, theirs = _split_argv(argv)

    if role is not None:
        # The supervisor spawned us. Everything except our own flags belongs to
        # the role, and it may include tokens (a probe name, a station_42 flag)
        # that our parser knows nothing about.
        parser = build_parser(add_help=False, with_command=False)
        args, unknown = parser.parse_known_args(ours)
        theirs = unknown
    else:
        # Our own flags are still useful to the role (--verbose, --transition),
        # so parse them out of whichever side they landed on.
        parser = build_parser(add_help=(command is None))
        args, unknown = parser.parse_known_args(ours if command else argv)
    args.command = command
    args.role = role

    if args.portable:
        os.environ.setdefault(
            "FS42_HOME",
            os.path.dirname(
                os.path.abspath(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
            ),
        )

    _prepare_environment()
    _configure_logging(args)

    from fs42 import paths

    if args.version:
        info = paths.describe()
        print(f"FieldStation42 {info['version']}")
        for key in ("frozen", "platform_tag", "resources", "data", "cache"):
            print(f"  {key}: {info[key]}")
        return 0

    resolved = role or _SUBCOMMANDS.get(command) or ROLE_SUPERVISOR

    from fs42.app import roles

    handler = roles.get(resolved)
    if handler is None:
        parser.error(f"Unknown role: {resolved}")
    return handler(args, theirs, unknown)
