"""What each process of the application actually runs.

Under ``spawn`` (which this fork forces on every platform) a role child is a
fresh interpreter that re-imports everything.  Keep these entry points cheap
at import time and do the heavy lifting inside the functions.
"""

import logging
import sys

_l = logging.getLogger("ROLE")


def supervisor_main(args, passthrough, extra) -> int:
    from fs42.app.supervisor import Supervisor

    return Supervisor(args, passthrough).run()


def player_main(args, passthrough, extra) -> int:
    """The TV itself: mpv, the schedule, channel changes, the OSD."""
    import field_player
    from fs42.reception import (
        long_change_effect,
        none_change_effect,
        short_change_effect,
    )

    transitions = {
        "long": long_change_effect,
        "short": short_change_effect,
        "none": none_change_effect,
    }
    transition = transitions.get(getattr(args, "transition", None) or "short")
    return field_player.run_player(transition_fn=transition, no_server=True)


def api_main(args, passthrough, extra) -> int:
    """Web console, web remote and REST API."""
    from fs42 import paths
    from fs42.fs42_server import fs42_server

    # Running the server on its own (FieldStation42 server) has to work on a
    # machine that has never run the supervisor.
    paths.first_run_seed()
    fs42_server.run_standalone()
    return 0


def osd_main(args, passthrough, extra) -> int:
    """Standalone on-screen display.

    The default OSD backend draws through mpv and therefore lives inside the
    player process; this role exists for the external-mpv case and for users
    who ran the OSD as its own service under upstream.
    """
    from fs42.osd import main as osd_module

    return osd_module.main(passthrough)


def tui_main(args, passthrough, extra) -> int:
    from fs42 import paths
    from fs42.ux.ux import StationApp

    paths.first_run_seed()
    StationApp().run()
    return 0


def cli_main(args, passthrough, extra) -> int:
    """Catalog/schedule build tooling (upstream's station_42.py)."""
    from fs42 import paths

    # Building a catalog is often the very first thing a new user does, so the
    # data directory has to exist by the time station_42 reads confs/.
    paths.first_run_seed()

    import station_42

    saved = sys.argv
    sys.argv = [saved[0]] + passthrough
    try:
        station_42.main()
    finally:
        sys.argv = saved
    return 0


def doctor_main(args, passthrough, extra) -> int:
    """Report what works on this machine and what does not."""
    from fs42.app import doctor

    return doctor.run()


def probe_main(args, passthrough, extra) -> int:
    """Run a single environment probe. Used by `doctor`, not by people."""
    from fs42.app import doctor

    name = (passthrough + extra + ["unknown"])[0]
    return doctor.run_probe(name)


def installer_main(args, passthrough, extra) -> int:
    """Graphical install / uninstall wizard (Linux and SteamOS)."""
    from fs42.app import setup_wizard

    return setup_wizard.run(args, passthrough + extra)


_ROLES = {
    "supervisor": supervisor_main,
    "player": player_main,
    "api": api_main,
    "osd": osd_main,
    "tui": tui_main,
    "cli": cli_main,
    "doctor": doctor_main,
    "probe": probe_main,
    "installer": installer_main,
}


def get(name):
    return _ROLES.get(name)
