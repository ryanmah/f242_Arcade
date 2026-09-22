"""On-screen display backends."""

import logging

from fs42.osd.backends.base import NullOSD, OSDBackend  # noqa: F401

_l = logging.getLogger("OSD")


def create_backend(mpv=None, name=None):
    """Build the configured OSD backend.

    ``osd_backend`` in main_config.json selects it: "mpv", "none", or "auto"
    (the default), which picks mpv whenever we own an mpv connection.
    """
    if name is None:
        try:
            from fs42.station_manager import StationManager

            name = StationManager().server_conf.get("osd_backend", "auto")
        except Exception:
            name = "auto"

    name = str(name or "auto").lower()

    if name == "none":
        return NullOSD()

    if name in ("auto", "mpv"):
        if mpv is None:
            if name == "mpv":
                _l.warning("The mpv OSD backend needs an mpv connection; disabling the OSD.")
            return NullOSD()
        from fs42.osd.backends.mpv_osd import MpvOSD

        return MpvOSD(mpv)

    _l.warning("Unknown osd_backend %r; disabling the OSD.", name)
    return NullOSD()
