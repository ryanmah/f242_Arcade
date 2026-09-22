"""Catalog and schedule jobs, shared by the web console and the in-app menu.

Upstream had these inline in ``fs42/fs42_server/api/build.py`` as closures
over a per-request task dict.  The in-app menu needs the same work with a
different progress sink, so the bodies live here and both callers hand in a
``log`` callable.

Each function is synchronous and slow (moviepy and ffprobe per file); callers
run them on a thread.  They return normally on success and raise on failure -
the caller decides how to report it.
"""

import logging

from fs42.catalog import ShowCatalog
from fs42.catalog_api import CatalogAPI
from fs42.liquid_manager import LiquidManager
from fs42.liquid_schedule import LiquidSchedule
from fs42.station_manager import StationManager

_l = logging.getLogger("BUILD")


def _stations_for(network_name):
    if not network_name or network_name == "all":
        return list(StationManager().stations)
    station = StationManager().station_by_name(network_name)
    if station is None:
        raise ValueError(f"No station named {network_name!r}")
    return [station]


def rebuild_catalog(network_name="all", log=None):
    """Delete and rebuild the catalog for one station or all of them.

    Schedules for those stations are reset first, as upstream does: a catalog
    rebuild invalidates the block plans built against the old one.
    """
    log = log or _l.info

    # Clear the fluid file cache dedup set so scan_file_cache runs fresh for
    # each unique content_dir.  Without this a second rebuild in the same
    # process silently skips every directory scan.
    ShowCatalog.clear_fluid_cache()

    log(f"Starting catalog rebuild for {network_name}")
    for station in _stations_for(network_name):
        name = station["network_name"]
        if station.get("_has_schedule"):
            log(f"Deleting schedule for {name}")
            LiquidManager().reset_schedule(station, False)
        if station.get("_has_catalog"):
            log(f"Rebuilding catalog for {name}")
            CatalogAPI.delete_catalog(station)
            ShowCatalog(station, rebuild_catalog=True)
            try:
                summary = CatalogAPI.get_summary(station)
                log(f"Rebuilt {name}: {summary.get('entry_count', 0)} clips")
            except Exception:
                log(f"Rebuilt catalog for {name}")
    log("Catalog rebuild complete.")


def add_schedule_time(amount="week", network_name="all", log=None):
    """Extend schedules by ``day``, ``week`` or ``month``."""
    log = log or _l.info
    if amount not in ("day", "week", "month"):
        raise ValueError(f"amount must be day, week or month, not {amount!r}")

    for station in _stations_for(network_name):
        if station.get("_has_schedule"):
            log(f"Adding a {amount} to the schedule for {station['network_name']}")
            LiquidSchedule(station).add_amount(amount)
    log(f"Added a {amount} to schedules.")


def reset_schedule(network_name="all", log=None):
    """Throw away the schedule (but not the catalog) and let it rebuild."""
    log = log or _l.info
    for station in _stations_for(network_name):
        if station.get("_has_schedule"):
            log(f"Resetting schedule for {station['network_name']}")
            LiquidManager().reset_schedule(station, True)
    log("Schedule reset complete.")


def delete_station_data(station, log=None):
    """Remove a station's catalog rows and schedule blocks.

    Upstream's delete only removes the config file and leaves both behind in
    the database, keyed by a network name that no longer exists.
    """
    log = log or _l.info
    name = station["network_name"]
    try:
        from fs42.liquid_api import LiquidAPI

        LiquidAPI.delete_blocks(station)
        log(f"Removed schedule for {name}")
    except Exception as e:
        log(f"Could not remove schedule for {name}: {e}")
    try:
        CatalogAPI.delete_catalog(station)
        log(f"Removed catalog for {name}")
    except Exception as e:
        log(f"Could not remove catalog for {name}: {e}")
