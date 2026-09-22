"""Standalone on-screen display process.

The default backend draws through mpv and therefore runs inside the player,
where the mpv connection lives.  This entry point exists for the case where
mpv is managed outside FieldStation42 (``start_mpv: false``) and for users who
ran the OSD as its own service under upstream.
"""

import logging
import time

from fs42 import ipc
from fs42 import paths
from fs42 import platform_compat
from fs42.osd.backends import create_backend

_l = logging.getLogger("OSD")

TICK_SECONDS = 1.0 / 30.0


def _connect_external_mpv():
    """Attach to an mpv instance we did not start."""
    from python_mpv_jsonipc import MPV

    from fs42.station_manager import StationManager

    endpoint = StationManager().server_conf.get("mpv_ipc_socket")
    if not endpoint:
        endpoint = platform_compat.mpv_ipc_name("shared")
    return MPV(start_mpv=False, ipc_socket=endpoint)


def main(argv=None) -> int:
    paths.first_run_seed()

    try:
        mpv = _connect_external_mpv()
    except Exception as e:
        _l.error("Could not attach to mpv: %s", e)
        _l.error(
            "The standalone OSD needs an mpv instance to draw into. If FieldStation42 "
            "starts mpv itself, the OSD already runs inside the player and this process "
            "is not needed."
        )
        return 1

    backend = create_backend(mpv)
    stop = {"now": False}
    platform_compat.install_shutdown_handlers(lambda signum: stop.__setitem__("now", True))

    _l.info("On-screen display running.")
    try:
        while not stop["now"] and not ipc.shutdown_requested():
            backend.tick()
            time.sleep(TICK_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
