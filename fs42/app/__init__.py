"""Application shell: entry point, CLI dispatch, process supervisor.

Everything here is new in this fork.  Upstream has no single entry point -
``field_player.py`` runs the TV, ``station_42.py`` runs the tooling and web
console, and the OSD is a separate systemd unit.  This package is what turns
those into roles of one executable.
"""

from fs42.app.cli import dispatch  # noqa: F401
