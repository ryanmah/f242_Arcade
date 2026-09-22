#!/usr/bin/env python3
"""FieldStation42 - single executable entry point.

Everything the application does is a role of this one program: the supervisor,
the TV player, the web console/API, the on-screen display, the text UI and the
catalog build tools.  Run it with no arguments to start the whole thing.

    FieldStation42                 # run the TV and the web console
    FieldStation42 server          # web console only
    FieldStation42 tui             # terminal admin UI
    FieldStation42 build --help    # catalog and schedule tooling
    FieldStation42 doctor          # check this machine's setup
"""

import multiprocessing
import sys


def _main() -> int:
    from fs42.app.cli import dispatch

    return dispatch(sys.argv[1:])


if __name__ == "__main__":
    # Must be the first thing that runs: under PyInstaller a spawned child
    # re-enters this file, and freeze_support() is what makes it hand control
    # to multiprocessing instead of starting a second application.
    multiprocessing.freeze_support()
    try:
        multiprocessing.set_start_method("spawn", force=True)
    except RuntimeError:
        pass
    sys.exit(_main())
