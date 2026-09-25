import logging
import os
import sys
from fs42 import paths
from fs42.slot_reader import SlotReader
from fs42.station_io import StationIO

class StationManager(object):
    # the borg singleton pattern
    __we_are_all_one = {}
    _initialized = False

    # public visible - be careful
    stations = []
    no_catalog = {"guide", "streaming", "web"}
    no_schedule = {"guide", "streaming", "web"}

    # NOTE: This is the borg singleton pattern - __we_are_all_one
    def __new__(cls, *args, **kwargs):
        obj = super(StationManager, cls).__new__(cls, *args, **kwargs)
        obj.__dict__ = cls.__we_are_all_one
        return obj

    def __init__(self):
        self.__dict__ = self.__we_are_all_one
        if not self._initialized:
            self._initialized = True
            if not len(self.stations):
                self.station_io = StationIO()
                self.server_conf = {
                    "channel_socket": str(paths.runtime("channel.socket")),
                    "status_socket": str(paths.runtime("play_status.socket")),
                    "day_parts": {
                        "morning": range(6, 10),
                        "daytime": range(10, 18),
                        "prime": range(18, 23),
                        "late": [23, 0, 1, 2],
                        "overnight": range(2, 6),
                    },
                    "time_format": "%H:%M",
                    "date_time_format": "%Y-%m-%dT%H:%M:%S",
                    "db_path": str(paths.runtime("fs42_fluid.db")),
                    "start_mpv": True,
                    "server_host": "0.0.0.0",
                    "server_port": 4242,
                    "title_patterns": [],
                    "video_seek_timeout": 10,
                    "standby_image": str(paths.runtime("standby.png")),
                    "osd_backend": "auto",
                    # Drop mpv out of fullscreen while the in-app menu is open.
                    # Off by default; the escape hatch for a window manager
                    # that will not put the menu above fullscreen video.
                    "menu_drop_fullscreen": False,
                    # The menu's "View" toggle: fullscreen or a window.
                    "fullscreen": True,
                    # Subtitles / closed captions; the menu's "Captions" row.
                    "captions": False,
                    # Poll a gamepad for menu navigation and channel changes.
                    "gamepad": False,
                    # Read the head and tail of what each channel plays next
                    # so the first tune to it is as quick as the second.
                    "prewarm_media": True,
                    # Keep every schedule at least a day ahead, building a
                    # week at a time in the background (upstream leaves this
                    # unset, so a schedule that runs out is only rebuilt when
                    # you tune to that channel).  Set to null to turn off.
                    "schedule_agent": {"amount_to_add": "week", "trigger_add_at": "day"},
                    # Controllers set up on the menu's Add Input pages, each
                    # with its own button map (see fs42/menu/gamepad.py).
                    # "gamepad_map" is the older single flat map.
                    "controllers": [],
                    "gamepad_map": {},
                    # CRT scanlines and noise over the video and the menu
                    # (see fs42/video_effects.py); set from the menu.
                    "video_effects": {"scanline_opacity": 0.0, "scanline_size": 3,
                                      "noise_opacity": 0.0, "noise_grain": 2},
                }
                self._number_index = {}
                self._name_index = {}
                self.load_main_config()
                self.load_json_stations()
            self.guide_config = None
            for i in range(len(self.stations)):
                station = self.stations[i]
                if station["network_type"] == "standard":
                    self.stations[i] = SlotReader.smooth_tags(station)

                if station["network_type"] == "guide":
                    self.guide_config = station
                    logging.getLogger().info("Loading and checking guide channel")
                    try:
                        from fs42.guide_tk import GuideWindowConf
                    except ImportError as e:
                        # tkinter is not installed. The guide channel will not
                        # work, but nothing else should be held hostage to it.
                        logging.getLogger().error(
                            "Guide channel needs tkinter, which is not available: %s", e
                        )
                        continue

                    gconf = GuideWindowConf()
                    errors = gconf.check_config(station)
                    if len(errors):
                        # Missing artwork or sound is not worth taking the
                        # whole station down for; the guide skips what it
                        # cannot load.  Say so loudly and carry on.
                        logging.getLogger().warning("Problems in the guide channel configuration (%s):", station.get("network_name"))
                        for err in errors:
                            logging.getLogger().warning("  %s", err)
                        station["_guide_problems"] = list(errors)
                    else:
                        logging.getLogger().info("Guide channel checks completed.")
                elif station["network_type"] == "web":
                    #determine if the URL is to a custom guide
                    if "static/customguide/customguide.html" in station["web_url"]:
                        self.guide_config = station
                        logging.getLogger().info("Setting web channel as guide channel")

    def station_by_name(self, name):
        if name in self._name_index:
            return self._name_index[name]
        return None

    def station_by_channel(self, channel_number):
        if channel_number in self._number_index:
            return self._number_index[channel_number]
        return None

    def index_from_channel(self, channel):
        index = 0
        for station in self.stations:
            if station["channel_number"] == channel:
                return index
            index += 1
        return None

    def get_day_parts(self):
        return self.server_conf["day_parts"]

    def load_main_config(self):
        _l = logging.getLogger("STATIONMANAGER")
        d = self.station_io.load_main_config()

        if d is not None:
            try:
                to_check = [
                    "channel_socket",
                    "status_socket",
                    "time_format",
                    "start_mpv",
                    "db_path",
                    "server_host",
                    "server_port",
                    "normalize_titles",
                    "tmdb_api_key",
                    "recall_last_channel",
                    "schedule_agent",
                    "video_seek_timeout",
                    "overlay_conf",
                    "start_channel",
                    # fork additions: bundled-binary overrides, OSD backend
                    # selection and the legacy runtime/*.socket mirror switch.
                    "mpv_path",
                    "ffprobe_path",
                    "osd_backend",
                    "legacy_socket_files",
                    # in-app menu
                    "menu_drop_fullscreen",
                    "fullscreen",
                    "captions",
                    "gamepad",
                    "gamepad_map",
                    "controllers",
                    "video_effects",
                    "prewarm_media",
                    "volume_step",
                ]

                for key in to_check:
                    if key in d:
                        self.server_conf[key] = d[key]

                # Paths that name files on disk are resolved against the user
                # data directory so existing relative configs keep working.
                for key in ("channel_socket", "status_socket", "db_path", "standby_image"):
                    if key in d and d[key]:
                        self.server_conf[key] = str(paths.resolve_user_path(d[key]))

                # Load custom title patterns if provided
                if "title_patterns" in d:
                    import re
                    custom_patterns = []
                    for i, pattern_config in enumerate(d["title_patterns"]):
                        try:
                            # Validate that required fields exist
                            if "pattern" not in pattern_config:
                                _l.error(f"title_patterns[{i}]: missing 'pattern' field")
                                continue
                            if "group" not in pattern_config:
                                _l.error(f"title_patterns[{i}]: missing 'group' field")
                                continue

                            # Validate that the regex compiles
                            re.compile(pattern_config["pattern"])

                            # Add as tuple (pattern, group) to match existing format
                            custom_patterns.append((pattern_config["pattern"], pattern_config["group"]))

                            desc = pattern_config.get("description", f"Custom pattern {i+1}")
                            _l.info(f"Loaded custom title pattern: {desc}")
                        except re.error as e:
                            _l.error(f"Invalid regex in title_patterns[{i}]: {e}")
                            _l.error(f"Pattern was: {pattern_config.get('pattern', 'N/A')}")

                    self.server_conf["title_patterns"] = custom_patterns
                    if custom_patterns:
                        _l.info(f"Loaded {len(custom_patterns)} custom title pattern(s)")

                if "day_parts" in d:
                    new_parts = {}
                    for key in d["day_parts"]:
                        start_hour = d["day_parts"][key]["start_hour"]
                        end_hour = d["day_parts"][key]["end_hour"]
                        if end_hour > start_hour:
                            new_parts[key] = range(start_hour, end_hour)
                        else:
                            # wraps midnight - manually build the list of hours
                            hours = []
                            hour = start_hour
                            while hour <= 23:
                                hours.append(hour)
                                hour += 1
                            hour = 0
                            while hour <= end_hour:
                                hours.append(hour)
                                hour += 1
                            new_parts[key] = hours
                    self.server_conf["day_parts"] = new_parts
                    

                if "date_time_format" not in d:
                    # check the environment variable or set default then
                    self.server_conf["date_time_format"] = os.environ.get("FS42_TS", "%Y-%m-%dT%H:%M:%S")
                else:
                    self.server_conf["date_time_format"] = d["date_time_format"]

            except Exception as e:
                print(e)
                _l.exception(e)
                _l.error(f"Error loading main config overrides from {self.station_io.main_config_path}")
                sys.exit(-1)
        # else: skip, no overrides (title_patterns already initialized to [] in __init__)

    def load_json_stations(self):
        """Load and index all station configurations."""
        _l = logging.getLogger("STATIONMANAGER")

        try:
            # Let StationIO do all the heavy lifting
            station_configs = self.station_io.load_and_process_all_stations()

            # Sort by channel number
            self.stations = sorted(station_configs, key=lambda station: station["channel_number"])

            # Build indexes
            self._build_indexes()
            self._confs_signature = self.confs_signature()

        except Exception as e:
            _l.error("*" * 60)
            _l.error("Error loading station configurations")
            _l.exception(e)
            _l.error("*" * 60)
            sys.exit(-1)

    def _build_indexes(self):
        """Build name and channel number indexes for fast lookup."""
        self._name_index = {}
        self._number_index = {}
        for station in self.stations:
            self._name_index[station["network_name"]] = station
            self._number_index[station["channel_number"]] = station

    def write_station_config(self, network_name, config_data, is_update=False):
        """
        Write a station configuration.
        Delegates to StationIO for all the work, then reloads.
        """
        # Let StationIO handle validation, uniqueness checks, and file writing
        success, message, file_path = self.station_io.save_station_config(
            network_name, config_data, self.stations, is_update
        )

        if success:
            # Reload the station configuration
            self._reload_stations()

        return success, message, file_path

    def delete_station_config(self, network_name):
        """
        Delete a station configuration.
        Delegates to StationIO for all the work, then reloads.
        """
        # Let StationIO handle existence checks and file deletion
        success, message = self.station_io.remove_station_config(network_name, self.stations)

        if success:
            # Reload stations
            self._reload_stations()

        return success, message

    def confs_signature(self):
        """A cheap fingerprint of confs/*.json: (count, newest mtime)."""
        import glob

        files = glob.glob(os.path.join(self.station_io.confs_dir, "*.json"))
        newest = 0.0
        for path in files:
            try:
                newest = max(newest, os.path.getmtime(path))
            except OSError:
                continue
        return (len(files), newest)

    def reload_if_changed(self) -> bool:
        """Reload when another process has edited the station files.

        StationManager is per process, so the web console, the in-app menu
        and the player each hold their own copy.  Callers that list stations
        for a user (the summary API, the menu) use this so they never show a
        stale list.  Returns True if a reload happened.
        """
        signature = self.confs_signature()
        if getattr(self, "_confs_signature", None) == signature:
            return False
        self._reload_stations()
        return True

    def _reload_stations(self):
        """Reload all station configurations from disk."""
        _l = logging.getLogger("STATIONMANAGER")
        _l.info("Reloading station configurations...")

        # Clear current stations and indexes
        self.stations = []
        self._name_index = {}
        self._number_index = {}

        # Reload from disk (this also rebuilds indexes)
        self.load_json_stations()

        # Re-apply tag smoothing for standard networks
        for i in range(len(self.stations)):
            station = self.stations[i]
            if station["network_type"] == "standard":
                self.stations[i] = SlotReader.smooth_tags(station)

        # Rebuild indexes after tag smoothing
        self._build_indexes()

        _l.info(f"Reloaded {len(self.stations)} station(s)")
