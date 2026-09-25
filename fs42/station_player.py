from enum import Enum
import logging
from pathlib import Path

import multiprocessing
import time
import datetime
import json
import os
import glob
import random
import logging
import time
from python_mpv_jsonipc import MPV

from fs42 import ipc
from fs42 import paths
from fs42 import platform_compat
# guide_tk pulls in tkinter, which not every install has and which only the
# guide channel needs.  Imported lazily so a missing tk never stops playback.
from fs42.autobump_agent import AutoBumpAgent

# Try to import web_render_runner, but handle gracefully if PySide6 (with QtWebEngine)
# isn't available -- web rendering is an optional feature.
try:
    from fs42.webrender.web_render import web_render_runner
    WEB_RENDER_AVAILABLE = True
except ImportError as e:
    logging.getLogger("station_player").info(
        "Web rendering disabled (PySide6 QtWebEngine not available): %s", e
    )
    WEB_RENDER_AVAILABLE = False
    web_render_runner = None

from fs42.reception import (
    ReceptionStatus,
    HLScrambledVideoFilter,
    DiagonalScrambledVideoFilter,
    ColorInvertedScrambledVideoFilter,
    ChunkyScrambledVideoFilter,
)
from fs42.liquid_manager import LiquidManager, PlayPoint, ScheduleNotFound, ScheduleQueryNotInBounds

from fs42.liquid_schedule import LiquidSchedule
from fs42.station_manager import StationManager
from fs42.slot_reader import SlotReader

logging.basicConfig(format="%(asctime)s %(levelname)s:%(name)s:%(message)s", level=logging.INFO)


STARTUP_FADE_SECONDS = 3.0


def _caption_options(on: bool) -> dict:
    if on:
        return {"sid": "auto", "sub_auto": "exact", "sub_visibility": True}
    return {"sid": "no", "sub_auto": "no", "sub_visibility": False}


def _guide_channel_runner():
    from fs42.guide_tk import guide_channel_runner

    return guide_channel_runner


def _guide_commands():
    from fs42.guide_tk import GuideCommands

    return GuideCommands



def update_status_socket(
    status, network_name, channel, title=None, timestamp="%Y-%m-%dT%H:%M:%S", duration=None, file_path=None, content_type=None
):
    status_obj = {
        "status": status,
        "network_name": network_name,
        "channel_number": channel,
        "timestamp": datetime.datetime.now().strftime(timestamp),
    }
    if title is not None:
        status_obj["title"] = title
    if duration is not None:
        status_obj["duration"] = duration
    if file_path is not None:
        status_obj["file_path"] = file_path
    if content_type is not None:
        status_obj["content_type"] = content_type
    # Published through the sqlite state bus; ipc mirrors it to the legacy
    # runtime/play_status.socket file for existing user scripts.
    ipc.set_status(status_obj)


class PlayerState(Enum):
    FAILED = 1
    EXITED = 2
    SUCCESS = 3
    CHANNEL_CHANGE = 4
    EXIT_COMMAND = 5
    PLAY_FILE = 6
    RELOAD_STATIONS = 7


class PlayerOutcome:
    def __init__(self, status=PlayerState.SUCCESS, payload=None):
        self.status = status
        self.payload = payload


class StationPlayer:
    MPV_RUNTIME_COMMANDS = {
        "toggle_subtitles": ("cycle", "sub-visibility"),
        "cycle_subtitles": ("cycle", "sub"),
        "cycle_audio": ("cycle", "audio"),
    }

    scramble_effects = {
        "horizontal_line": "lavfi=[geq='if(mod(floor(Y/4),2),p(X,Y+20*sin(2*PI*X/50)),p(X,Y))']",
        "diagonal_lines": "lavfi=[geq='p(X+10*sin(2*PI*Y/30),Y)']",
        "static_overlay": "lavfi=[geq='if(gt(random(X+Y*W),0.85),128+127*random(X*Y),if(mod(floor(Y/4),2),p(X+20,Y),p(X,Y)))']",
        "pixel_block": "lavfi=[scale=160:120,scale=640:480:flags=neighbor,geq='if(gt(random(floor(X/40)*floor(Y/30)),0.7),128,p(X,Y))']",
        "color_inversion": "lavfi=[geq='if(mod(floor(Y/16),2),255-p(X,Y),p(X,Y))']",
        "severe_noise": "lavfi=[geq='if(gt(random(X*Y),0.7),random(255),p(X,Y))']",
        "wavy": "lavfi=[geq='p(X+15*sin(2*PI*Y/40),Y+10*cos(2*PI*X/60))']",
        "random_block": "lavfi=[geq='if(gt(random(floor(X/20)*floor(Y/20)),0.6),p(X+random(100)-20,Y+random(70)-20),p(X,Y))']",
        "chunky_scramble": "lavfi=[scale=320:240,split[base][aux];[aux]geq=r='p(X+floor((random(1000+floor(N*0.05)+floor(Y/16))-0.5)*W*0.4),Y)':g='p(X+floor((random(2000+floor(N*0.05)+floor(Y/16))-0.5)*W*0.4),Y)':b='p(X+floor((random(3000+floor(N*0.05)+floor(Y/16))-0.5)*W*0.4),Y)'[warped];[base][warped]overlay,scale=640:480]",
        "spicy" : (
            "lavfi=["
            "scale=720:480:flags=fast_bilinear,"
            "noise=alls=12:allf=t,"
            "eq=contrast=1.10:brightness=-0.04:saturation=2.4,"
            "geq="
            "r='p(X + 40*sin(2*PI*Y/60) + 12*sin(2*PI*N/10), Y)':"
            "g='p(X + 32*sin(2*PI*Y/64) + 10*sin(2*PI*N/11), Y)':"
            "b='p(X + 24*sin(2*PI*Y/68) +  8*sin(2*PI*N/12), Y)',"
            "scale=640:480:flags=neighbor"
            "]"
        ),
        "special_sauce" : (
            "lavfi=["
            "scale=360:240:flags=fast_bilinear,"
            "format=gbrp,"
            "geq="
            "r='p(mod(X + 80*sin(2*PI*Y/45 + N*0.4) + 20*sin(2*PI*Y/13 + N*0.7),W), Y)':"
            "g='p(mod(X + 70*sin(2*PI*Y/48 + N*0.43) + 17*sin(2*PI*Y/14 + N*0.73),W), Y)':"
            "b='p(mod(X + 60*sin(2*PI*Y/50 + N*0.46) + 13*sin(2*PI*Y/15 + N*0.76),W), Y)'"
            ":interpolation=nearest,"
            "noise=alls=15:allf=t,"
            "eq=contrast=1.2:brightness=-0.05:saturation=1.2,"
            "format=yuv420p,"
            "scale=640:480:flags=neighbor"
            "]"
        ),
        "glitchtastic" : (
            "lavfi=["
            "scale=360:240:flags=fast_bilinear,"
            "format=gbrp,"
            "geq="
            "r='if(gt(pow(abs(sin(N*0.08)*sin(N*0.11)),0.15),0.7),"
            "p(mod(X+80*sin(2*PI*Y/45+N*0.4)+20*sin(2*PI*Y/13+N*0.7),W),Y),"
            "if(gt(mod(Y+N*4,H),H-30),255-p(X,Y),"
            "p(X+15*sin(2*PI*Y/45+N*0.4),Y)))':"
            "g='if(gt(pow(abs(sin(N*0.08)*sin(N*0.11)),0.15),0.7),"
            "p(mod(X+70*sin(2*PI*Y/48+N*0.43)+17*sin(2*PI*Y/14+N*0.73),W),Y),"
            "if(gt(mod(Y+N*4,H),H-30),255-p(X,Y),"
            "p(X+12*sin(2*PI*Y/48+N*0.43),Y)))':"
            "b='if(gt(pow(abs(sin(N*0.08)*sin(N*0.11)),0.15),0.7),"
            "p(mod(X+60*sin(2*PI*Y/50+N*0.46)+13*sin(2*PI*Y/15+N*0.76),W),Y),"
            "if(gt(mod(Y+N*4,H),H-30),255-p(X,Y),"
            "p(X+10*sin(2*PI*Y/50+N*0.46),Y)))'"
            ":interpolation=nearest,"
            "noise=alls=15:allf=t,"
            "eq=contrast=1.2:brightness=-0.05:saturation=1.2,"
            "format=yuv420p,"
            "scale=640:480:flags=neighbor"
            "]"
        )
    }

    audio_scramble_effects = {
        "special_sauce": "lavfi=[afreqshift=shift=-2000,vibrato=f=2.5:d=0.4,volume=0.8]",
        "the_jitters": "lavfi=[afreqshift=shift=-3000,aecho=0.6:0.5:50|80:0.4|0.3,tremolo=f=8:d=0.9,volume=0.7]",
        "possessed": "lavfi=[chorus=0.3:0.3:40|60|80:0.4|0.3|0.2:0.8|1.2|1.6:3|4|5,afreqshift=shift=-1500,lowpass=f=2500,volume=0.8]",
        "demonic": "lavfi=[rubberband=pitch=0.5,chorus=0.3:0.3:40|60:0.4|0.3:1.0|1.4:3|4,volume=1.0]",
        "slightly_borked": "lavfi=[acrusher=bits=3:samples=12:lfo=true:lforange=50:lforate=0.3,vibrato=f=3:d=0.5,volume=0.25]"
    }

    def __init__(self, station_config, input_check_fn, mpv=None):
        self._l = logging.getLogger("FieldPlayer")

        start_it = True

        if "start_mpv" in StationManager().server_conf:
            start_it = StationManager().server_conf["start_mpv"]

        if not mpv:
            self._l.info("Starting MPV instance")

            # python-mpv-jsonipc adds the \\.\pipe\ prefix itself on Windows,
            # so this must be a bare name there and a real path elsewhere.
            self.ipc_endpoint = platform_compat.mpv_ipc_name()
            mpv_binary = paths.bin_path("mpv") if start_it else None
            if start_it and mpv_binary is None:
                self._l.error(
                    "Could not find mpv. Install it or set \"mpv_path\" in %s",
                    paths.confs("main_config.json"),
                )

            mpv_kwargs = {
                "start_mpv": start_it,
                "ipc_socket": self.ipc_endpoint,
                "input_default_bindings": False,
                # "View: Fullscreen / Windowed" on the menu.
                "fs": bool(StationManager().server_conf.get("fullscreen", True)),
                # A window, when there is one, never bigger than the screen.
                "autofit_larger": "80%x80%",
                # Captions: off unless turned on from the menu.  sid=no stops
                # mpv picking an embedded or side-by-side subtitle track at
                # all; sub-visibility hides one even if a remote selects it.
                **_caption_options(bool(StationManager().server_conf.get("captions", False))),
                "idle": True,
                "force_window": True,
                "script_opts": "osc-idlescreen=no",
                "hr_seek": "yes",
            }
            if mpv_binary is not None:
                mpv_kwargs["mpv_location"] = str(mpv_binary)
            if start_it and self._mpv_supports(mpv_binary, "osd-fonts-dir"):
                # Lets the OSD (channel number and name) use the same VCR
                # face as the menu without installing the font.  mpv 0.40+;
                # older builds only know sub-fonts-dir, which leaves the OSD
                # on its default face.
                mpv_kwargs["osd_fonts_dir"] = str(paths.menu_fonts_dir())
            if start_it:
                # Closing the mpv window or pressing q is the only "I want out"
                # affordance in a windowed build with no console.
                mpv_kwargs["quit_callback"] = self._on_mpv_quit

            self.mpv = MPV(**mpv_kwargs)
            if start_it:
                self._bind_menu_keys()
                self._focus_video_window()
                self.apply_video_effects()
        else:
            self.mpv = mpv
            self.ipc_endpoint = None

        self.station_config = station_config
        # self.playlist = self.read_json(runtime_filepath)
        self.input_check_fn = input_check_fn
        self.index = 0
        self.reception = ReceptionStatus()
        self.current_playing_file_path = None
        self.skip_reception_check = False
        self.web_process = None
        self.web_queue = None
        self.guide_process = None
        self.guide_queue = None
        self.overlay_process = None
        self.overlay_queue = None
        self._guide_showing = False
        self._overlay_supported = None
        self.scrambler = None
        self.now_playing_process = None
        self.schedule_lock = None
        self._active_afx = None
        self._pending_response = None
        self.osd = None
        self.menu_process = None
        self._menu_fullscreen_dropped = False
        self._fullscreen = bool(StationManager().server_conf.get("fullscreen", True))
        # Fade the very first picture in from black instead of flashing the
        # standby card at start-up.
        self._startup_fade = True
        self._gamepad = None
        if StationManager().server_conf.get("gamepad"):
            self._start_gamepad()
        self._prewarmer = None
        if StationManager().server_conf.get("prewarm_media", True):
            try:
                from fs42.prewarm import Prewarmer

                self._prewarmer = Prewarmer()
                self._prewarmer.start()
            except Exception as e:
                self._l.warning("Media pre-warm unavailable: %s", e)

    # ------------------------------------------------ effects over the guide

    def _overlay_alive(self):
        process = getattr(self, "overlay_process", None)
        return process is not None and process.is_alive()

    def _overlay_send(self, message, values=None):
        """Show/hide/update the see-through CRT layer used over the guide."""
        from fs42 import video_effects

        values = video_effects.clean(values) if values is not None else video_effects.load()
        if message == "show" and video_effects.is_off(values):
            message = "hide"
        if message in ("hide", "values") and not self._overlay_alive():
            return
        if message == "show" and not self._overlay_alive():
            if self._overlay_supported is None:
                from fs42.effects_overlay import compositing_available

                self._overlay_supported = compositing_available()
                if not self._overlay_supported:
                    self._l.info("No compositor: video effects will not cover the guide channel")
            if not self._overlay_supported:
                return
            try:
                from fs42.effects_overlay import _overlay_entry

                self.overlay_queue = multiprocessing.Queue()
                self.overlay_process = multiprocessing.Process(
                    target=_overlay_entry, args=(self.overlay_queue,), name="fs42-effects")
                self.overlay_process.daemon = True
                self.overlay_process.start()
            except Exception as e:
                self._l.warning("Could not start the effects overlay: %s", e)
                self.overlay_process = None
                return
        try:
            if message == "hide":
                self.overlay_queue.put(("hide",))
            else:
                self.overlay_queue.put((message, values))
        except Exception:
            pass

    def _stop_overlay(self):
        if self.overlay_process is None:
            return
        try:
            if self.overlay_process.is_alive():
                self.overlay_queue.put(("exit",))
                self.overlay_process.join(timeout=2)
            if self.overlay_process.is_alive():
                self.overlay_process.terminate()
        except Exception:
            pass
        self.overlay_process = None
        self.overlay_queue = None

    def apply_video_effects(self, values=None):
        """Put the CRT scanline/noise shader on mpv (saved settings by default)."""
        from fs42 import video_effects

        values = video_effects.clean(values) if values is not None else video_effects.load()
        # (Called once from __init__ before the overlay attributes exist.)
        if getattr(self, "_guide_showing", False):
            self._overlay_send("show", values)
        elif hasattr(self, "overlay_process"):
            self._overlay_send("values", values)
        if self.mpv is None:
            return
        if video_effects.apply_to_mpv(self.mpv, values):
            self._l.info("Video effects: scanlines %.0f%% every %dpx, noise %.0f%% at %dpx",
                         values["scanline_opacity"] * 100, values["scanline_size"],
                         values["noise_opacity"] * 100, values["noise_grain"])

    @staticmethod
    def _mpv_supports(binary, option: str) -> bool:
        """Whether this mpv knows an option (only probed for non-bundled mpv)."""
        if binary is None:
            return False
        try:
            if paths.bin_dir() in Path(binary).parents:
                return True         # the bundled build is recent enough
            result = platform_compat.run_hidden(
                [str(binary), "--no-config", "--list-options"], capture_output=True, text=True, timeout=10
            )
            return f"--{option}" in (result.stdout or "")
        except Exception:
            return False

    def _on_mpv_quit(self):
        """mpv window closed by the user - bring the whole app down with it."""
        self._l.info("mpv exited - requesting application shutdown")
        try:
            ipc.request_shutdown("mpv_quit")
        except Exception:
            pass

    def attach_osd(self):
        """Create the on-screen display bound to this mpv instance."""
        try:
            from fs42.osd.backends import create_backend

            self.osd = create_backend(self.mpv)
        except Exception as e:
            self._l.warning("On-screen display unavailable: %s", e)
            self.osd = None
        return self.osd

    def tick_osd(self):
        self.poll_menu()
        if self.osd is not None:
            try:
                self.osd.tick()
            except Exception as e:
                self._l.debug("OSD tick failed: %s", e)
                self.osd = None

    def set_volume(self, action):
        """Volume through mpv rather than the system mixer.

        Upstream shelled out to amixer/pactl/wpctl, none of which exist on
        Windows.  Driving mpv's own properties also scopes volume to the TV
        rather than the whole machine, which is what an appliance should do.
        """
        step = StationManager().server_conf.get("volume_step", 5)
        try:
            current = self.mpv.volume
            current = 100 if current is None else float(current)
            if action == "up":
                self.mpv.volume = min(100.0, current + step)
            elif action == "down":
                self.mpv.volume = max(0.0, current - step)
            elif action == "mute":
                self.mpv.mute = not bool(self.mpv.mute)
            muted = bool(self.mpv.mute)
            level = int(float(self.mpv.volume or 0))
        except Exception as e:
            self._l.warning("Volume command '%s' failed: %s", action, e)
            return None

        response = {
            "volume": f"{level}%",
            "level": level,
            "muted": muted,
            "method": "mpv",
        }
        ipc.set_volume(response)
        return response

    # ------------------------------------------------------------ in-app menu

    def _mpv_pid(self):
        try:
            return self.mpv.mpv_process.process.pid
        except AttributeError:
            return None

    def _focus_video_window(self):
        """Give the video window keyboard focus (Windows).

        Escape, the arrows and the number keys are read by mpv, so the
        window must own the keyboard; Windows does not hand focus to windows
        opened by background processes, which every one of ours is.
        """
        if platform_compat.IS_WINDOWS and not os.environ.get("FS42_NO_FOCUS"):
            platform_compat.focus_window_of_pid_soon(self._mpv_pid())

    def _bind_menu_keys(self):
        """Route keys pressed on the mpv window to the menu.

        mpv owns the fullscreen window, so this is where a keyboard (or an IR
        remote that presents as one) lands.  Escape opens the menu; while it is
        open, navigation keys are forwarded over the state bus so the menu works
        even if the window manager never hands the Qt window keyboard focus.
        The callbacks run on the IPC thread, so they only push messages.
        """
        from fs42.menu.input import MPV_KEY_ACTIONS, Action

        # mpv re-fires a held key at its autorepeat rate.  Navigation may
        # repeat, but slowly; anything that opens, closes or activates must
        # not, or one long press on Escape opens and closes the menu in a loop.
        navigation = {Action.UP.value, Action.DOWN.value, Action.LEFT.value, Action.RIGHT.value,
                      Action.PAGE_UP.value, Action.PAGE_DOWN.value}
        last_fired = {}

        def make(action):
            def callback():
                now = time.monotonic()
                try:
                    menu_open = bool(ipc.get_state(ipc.KEY_MENU_OPEN))
                except Exception:
                    menu_open = False
                gap = 0.12 if (action in navigation and menu_open) else 0.35
                if now - last_fired.get(action, 0.0) < gap:
                    return
                last_fired[action] = now
                try:
                    if menu_open:
                        ipc.push(ipc.TOPIC_MENU_INPUT, {"action": action})
                    elif action == Action.BACK.value:
                        # Escape with no menu up: open it.
                        ipc.push(ipc.TOPIC_PLAYER_CMD, {"command": "menu"})
                    elif action in (Action.UP.value, Action.DOWN.value):
                        # No menu: the arrow keys surf channels, like the
                        # controller's D-pad.
                        ipc.push(ipc.TOPIC_CHANNEL, {"command": "up" if action == Action.UP.value else "down"})
                except Exception as e:
                    self._l.debug("menu key forward failed: %s", e)
            return callback

        try:
            for key, action in MPV_KEY_ACTIONS.items():
                self.mpv.bind_key_press(key, make(action))
        except Exception as e:
            self._l.warning("Could not bind menu keys on mpv: %s", e)

    def _start_gamepad(self):
        try:
            from fs42.menu.gamepad import GamepadReader

            self._gamepad = GamepadReader()
            self._gamepad.start()
        except Exception as e:
            self._l.warning("Gamepad support unavailable: %s", e)

    def _restart_gamepad(self):
        """Pick up a new button map (or a freshly enabled controller)."""
        if self._prewarmer is not None:
            try:
                self._prewarmer.stop()
            except Exception:
                pass
            self._prewarmer = None
        if self._gamepad is not None:
            try:
                self._gamepad.stop()
            except Exception:
                pass
            self._gamepad = None
        try:
            from fs42.station_io import StationIO

            config = StationIO().load_main_config() or {}
        except Exception:
            config = {}
        if config.get("gamepad", StationManager().server_conf.get("gamepad")):
            self._l.info("Restarting the gamepad reader")
            self._start_gamepad()

    def menu_is_open(self) -> bool:
        return bool(self.menu_process is not None and self.menu_process.is_alive())

    def open_menu(self):
        if self.menu_is_open():
            return
        from fs42.menu.app import run_menu

        self._l.info("Opening the channel menu")
        ipc.set_state(ipc.KEY_MENU_OPEN, True)
        ipc.set_state(ipc.KEY_INPUT_CAPTURE, False)
        if StationManager().server_conf.get("menu_drop_fullscreen"):
            try:
                self.mpv.fs = False
                self.mpv.ontop = False
                self._menu_fullscreen_dropped = True
            except Exception:
                pass
        self.menu_process = run_menu()

    def close_menu(self):
        """Called once the menu process has exited."""
        if self.menu_process is not None:
            try:
                self.menu_process.join(timeout=0.5)
            except Exception:
                pass
        self.menu_process = None
        ipc.set_state(ipc.KEY_MENU_OPEN, False)
        ipc.set_state(ipc.KEY_INPUT_CAPTURE, False)
        self._focus_video_window()
        if self._menu_fullscreen_dropped:
            try:
                self.mpv.fs = self._fullscreen
            except Exception:
                pass
            self._menu_fullscreen_dropped = False

    def poll_menu(self):
        """Notice the menu closing.  Cheap enough for the 50ms wait loops."""
        if self.menu_process is not None and not self.menu_process.is_alive():
            self.close_menu()

    def reload_stations(self):
        """Re-read station configs written by the menu or the web console.

        StationManager is a per-process singleton, so edits made elsewhere are
        invisible here until this runs.  Upstream never does it; a running
        player did not learn about new stations until restart.
        """
        manager = StationManager()
        manager._reload_stations()
        # __init__ is the only place upstream sets guide_config.
        manager.guide_config = None
        for station in manager.stations:
            if station["network_type"] == "guide":
                manager.guide_config = station
            elif station["network_type"] == "web" and "static/customguide/customguide.html" in station.get("web_url", ""):
                manager.guide_config = station
        LiquidManager().reload_schedules()
        self._l.info("Reloaded %d station(s)", len(manager.stations))

    def load_up(self):
        start_time = time.perf_counter()
        liquid = LiquidManager()
        liquid_init_time = time.perf_counter() - start_time
        self._l.info(f"LiquidManager() initialization took {liquid_init_time:.3f} seconds")
        self.warm_guide()

    def show_text(self, text, duration=4):
        self.mpv.command("show-text", text, duration)

    def mpv_runtime_command(self, action):
        """Run a small set of user-facing mpv runtime commands."""
        # Volume is a property write rather than a command, and it needs to
        # publish the new level for the on-screen meter.
        if action.startswith("volume_"):
            result = self.set_volume(action.split("_", 1)[1])
            if result is not None and self.osd is not None:
                self.tick_osd()
            return result is not None

        mpv_command = self.MPV_RUNTIME_COMMANDS.get(action)
        if not mpv_command:
            self._l.warning(f"Unknown mpv runtime command: {action}")
            return False

        try:
            self.mpv.command(*mpv_command)
            self._l.info(f"Ran mpv runtime command: {action}")
            return True
        except Exception as e:
            self._l.warning(f"Failed to run mpv runtime command {action}: {e}")
            return False

    def handle_runtime_command_outcome(self, response):
        """Handle non-interrupting player commands and continue playback."""
        if (
            response
            and response.status == PlayerState.SUCCESS
            and isinstance(response.payload, str)
            and response.payload.startswith("mpv_command:")
        ):
            action = response.payload.split(":", 1)[1]
            self.mpv_runtime_command(action)
            return True

        if (
            response
            and response.status == PlayerState.SUCCESS
            and response.payload == "menu:open"
        ):
            self.open_menu()
            return True

        if (
            response
            and response.status == PlayerState.SUCCESS
            and response.payload == "input:reload"
        ):
            self._restart_gamepad()
            return True

        if (
            response
            and response.status == PlayerState.SUCCESS
            and isinstance(response.payload, str)
            and response.payload.startswith("captions:")
        ):
            on = response.payload.split(":", 1)[1] == "on"
            try:
                for name, value in _caption_options(on).items():
                    setattr(self.mpv, name, value)
            except Exception as e:
                self._l.warning("Could not change captions: %s", e)
            self._l.info("Captions %s", "on" if on else "off")
            return True

        if (
            response
            and response.status == PlayerState.SUCCESS
            and isinstance(response.payload, str)
            and response.payload.startswith("view:")
        ):
            self._fullscreen = response.payload.split(":", 1)[1] == "fullscreen"
            try:
                self.mpv.fs = self._fullscreen
            except Exception as e:
                self._l.warning("Could not change the view: %s", e)
            self._l.info("View: %s", "fullscreen" if self._fullscreen else "windowed")
            return True

        if (
            response
            and response.status == PlayerState.SUCCESS
            and isinstance(response.payload, str)
            and response.payload.startswith("picture:")
        ):
            # Live preview from the menu, only for the channel on screen.
            from fs42 import picture

            try:
                message = json.loads(response.payload.split(":", 1)[1])
            except ValueError:
                message = {}
            if (self.station_config or {}).get("network_name") == message.get("network_name"):
                values = message.get("values")
                if values is None:
                    picture.apply_station(self.mpv, self.station_config)
                else:
                    picture.apply_to_mpv(self.mpv, values, panscan=self.station_config.get("panscan"))
            return True

        if (
            response
            and response.status == PlayerState.SUCCESS
            and isinstance(response.payload, str)
            and response.payload.startswith("video_effects:")
        ):
            try:
                values = json.loads(response.payload.split(":", 1)[1])
            except ValueError:
                values = None
            self.apply_video_effects(values)
            return True

        return False

    def _close_now_playing(self):
        # terminate the Now Playing overlay if it's running
        if self.now_playing_process and self.now_playing_process.is_alive():
            try:
                self.now_playing_process.terminate()
                self.now_playing_process.join(timeout=0.2)

                # Force kill if still alive
                if self.now_playing_process.is_alive():
                    self.now_playing_process.kill()
                    self.now_playing_process.join(timeout=0.1)

                self._l.debug("Closed Now Playing overlay")
            except Exception as e:
                self._l.warning(f"Error terminating overlay: {e}")

            self.now_playing_process = None

    def _nfo_overlay_data(self, file_path):

        from fs42.metadata_io import MetadataIO
        from fs42.nfo_agent import NFOAgent, NFOData

        meta = MetadataIO.read(file_path)
        lines = NFOAgent.overlay_lines(meta)
        return NFOData(lines) if lines else None

    def _show_now_playing(self, file_path):
        # show the Now Playing overlay for an audio file
        import time
        self._close_now_playing()
        # give a brief moment for cleanup
        time.sleep(0.1)

        # Start new overlay
        try:
            from fs42.overlay.now_playing import run_now_playing
            db_path = StationManager().server_conf["db_path"]
            self.now_playing_process = run_now_playing(file_path, db_path)
            self._l.info(f"Started Now Playing overlay for {file_path}")
        except Exception as e:
            self._l.error(f"Failed to start Now Playing overlay: {e}")

    def _show_stream_down(self):
        """Stop playback and display a stream-unavailable OSD message."""
        message = "TECHNICAL DIFFICULTIES"
        if self.station_config:
            message = self.station_config.get("stream_down_message", message)
        self._l.warning(f"Stream down — showing fallback: {message}")
        try:
            self.mpv.command("playlist-clear")
            self.mpv.stop()
        except Exception:
            pass
        try:
            self.mpv.command("show-text", message, 10000)
        except Exception as e:
            self._l.debug(f"Could not show stream down OSD: {e}")

    def shutdown(self):
        self.current_playing_file_path = None
        try:
            from fs42 import video_effects

            video_effects.cleanup_shader_files()
        except Exception:
            pass
        # Terminate any running web process
        if self.web_process and self.web_process.is_alive():
            self._l.info("Terminating web process")
            try:
                if self.web_queue:
                    self.web_queue.put("hide_window")
                self.web_process.join(timeout=2)
            except Exception:
                pass

            # Check if process is still alive and has valid _popen
            if self.web_process and hasattr(self.web_process, '_popen') and self.web_process._popen and self.web_process.is_alive():
                try:
                    self.web_process.terminate()
                    self.web_process.join(timeout=1)
                except Exception:
                    pass

        self.web_process = None
        self.web_queue = None
        self._stop_guide_process()
        self._stop_overlay()

        # Terminate any running now playing overlay
        self._l.info("Terminating now playing overlay")
        self._close_now_playing()

        if self.menu_is_open():
            try:
                self.menu_process.terminate()
                self.menu_process.join(timeout=1)
            except Exception:
                pass
        ipc.set_state(ipc.KEY_MENU_OPEN, False)
        if self._gamepad is not None:
            try:
                self._gamepad.stop()
            except Exception:
                pass

        if self.osd is not None:
            try:
                self.osd.close()
            except Exception:
                pass
            self.osd = None

        try:
            self.mpv.terminate()
        except Exception as e:
            self._l.debug("mpv terminate raised: %s", e)
        platform_compat.terminate_ipc_endpoint(getattr(self, "ipc_endpoint", None))

    def update_filters(self):
        self.mpv.vf = self.reception.filter()

    def update_reception(self):
        if not self.reception.is_perfect():
            self.reception.improve()
            # did that get us below threshhold?
            if self.reception.is_perfect():
                self.mpv.vf = ""
            else:
                self.mpv.vf = self.reception.filter()

    def play_file(self, file_path, file_duration=None, offset_seconds=None, is_stream=False, title="Unknown", content_type=None, media_type=None):
        try:
            if not is_stream and not AutoBumpAgent.is_autobump_url(file_path):
                # Catalogs built elsewhere name files by where they were then.
                located = paths.locate_media(file_path)
                if located != file_path:
                    self._l.debug(f"{file_path} found at {located}")
                    file_path = located
            if os.path.exists(file_path) or is_stream or AutoBumpAgent.is_autobump_url(file_path):
                self._l.debug(f"%%%Attempting to play {file_path}")
                self.current_playing_file_path = file_path

                if self.station_config:
                    self._l.debug("Got station config, updating status socket")
                    if "date_time_format" in StationManager().server_conf:
                        ts_format = StationManager().server_conf["date_time_format"]
                    else:
                        ts_format = "%Y-%m-%dT%H:%M:%S"
                    duration = (
                        f"{str(datetime.timedelta(seconds=int(offset_seconds)))}/{str(datetime.timedelta(seconds=int(file_duration)))}"
                        if file_duration
                        else "n/a"
                    )
                    update_status_socket(
                        "playing",
                        self.station_config["network_name"],
                        self.station_config["channel_number"],
                        title,
                        timestamp=ts_format,
                        duration=duration,
                        file_path=file_path,
                        content_type=content_type,
                    )
                          
                else:
                    self._l.warning(
                        "station_config not available in play_file, cannot update status socket with title."
                    )
                
                                #now see if this is an autobump

                if AutoBumpAgent.is_autobump_url(file_path):
                    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
                    web_url = AutoBumpAgent.extract_url(file_path)
                    remaining = file_duration - (offset_seconds or 0) if file_duration else None
                    if remaining is not None:
                        parsed = urlparse(web_url)
                        params = parse_qs(parsed.query, keep_blank_values=True)
                        params['duration'] = [str(int(remaining * 1000))]
                        web_url = urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in params.items()})))
                    conf = {
                        "web_url": web_url
                    }
                    if remaining:
                        conf["duration"] = remaining
                    self.show_web(conf, blocking=False)
                    return True

                # Per-channel scaling and zoom (fs42/picture.py).
                from fs42 import picture

                picture.apply_station(self.mpv, self.station_config)

                self._apply_vfx(datetime.datetime.now())

                # self.mpv.vf = "lavfi=[]"
                self._l.info(f"playing {file_path}")
                fading = self._startup_fade and self.osd is not None and hasattr(self.osd, "cover")
                if fading:
                    # Black until the first frame is ready, then fade up.
                    try:
                        self.osd.cover()
                    except Exception:
                        fading = False
                self.mpv.command("playlist-clear")
                self.mpv.play(file_path)
                

                timeout_seconds = StationManager().server_conf.get("video_seek_timeout", 10)
                start_time = time.time()

                while True:
                    try:
                        if self.mpv.time_pos is not None:
                            break
                        if time.time() - start_time > timeout_seconds:
                            self._l.error(f"Timeout waiting for playback to start on {file_path}")
                            return False
                        if is_stream:
                            response = self.input_check_fn()
                            if response and not self.handle_runtime_command_outcome(response):
                                self._pending_response = response
                                return False
                        self.tick_osd()
                        time.sleep(0.05)
                    except Exception as e:
                        if time.time() - start_time > timeout_seconds:
                            self._l.error(f"Error waiting for playback: {e}")
                            return False
                        self.tick_osd()
                        time.sleep(0.05)

                # Perform seek if needed (before showing overlay)
                if not is_stream and offset_seconds is not None and offset_seconds > 0:
                    self._seek_with_verify(file_path, offset_seconds, timeout_seconds)

                if fading:
                    self._startup_fade = False
                    try:
                        self.osd.fade_in(STARTUP_FADE_SECONDS)
                    except Exception as e:
                        self._l.debug("fade-in failed: %s", e)

                # Show Now Playing overlay for audio feature files
                self._l.info(f"Media type: {media_type}, Content type: {content_type}")
                if media_type == 'audio' and content_type == 'feature':
                    self._show_now_playing(file_path)
                elif media_type == 'video':
                    # Always close any existing overlay when a new video starts,
                    # then spawn a new one only if this video has an NFO sidecar.
                    self._close_now_playing()
                    try:
                        nfo_data = self._nfo_overlay_data(file_path)
                        if nfo_data:
                            play_duration = None
                            if file_duration is not None:
                                play_duration = file_duration - (offset_seconds or 0)
                            from fs42.nfo_agent import NFOAgent
                            self.now_playing_process = NFOAgent.show_overlay(nfo_data, play_duration=play_duration)
                    except Exception as e:
                        self._l.warning(f"Could not start NFO overlay: {e}")

                return True
            else:
                self._l.error(
                    f"Trying to play file {file_path} but it doesn't exist - check your configuration and try again."
                )
                return False
        except Exception as e:
            self._l.exception(e)
            self._l.error(
                f"Encountered unknown error attempting to play {file_path} - please check your configurations."
            )
            return False

    def _seek_with_verify(self, file_path, offset_seconds, timeout_seconds,
                          tolerance=1.0, verify_window=2.0, retry_delay=0.2):

        def safe_prop(name, default=None):
            try:
                return getattr(self.mpv, name)
            except Exception:
                return default

        deadline = time.time() + timeout_seconds
        attempt = 0

        while time.time() < deadline:
            attempt += 1

            try:
                self.mpv.command("seek", offset_seconds, "absolute")
            except Exception as e:
                self._l.debug(f"Seek not accepted yet on {file_path}: {e} (attempt {attempt})")
                time.sleep(retry_delay)
                continue

            patience = time.time() + verify_window
            while time.time() < deadline:
                pos = safe_prop("time_pos")
                if pos is not None and abs(pos - offset_seconds) <= tolerance:
                    self._l.info(f"Seek landed at {pos:.2f} (target {offset_seconds}, attempt {attempt})")
                    return
                if safe_prop("seeking"):
                    patience = time.time() + verify_window
                elif time.time() >= patience:
                    break  # idle and not landed -> dropped, re-issue
                self.tick_osd()
                time.sleep(0.05)

            self._l.debug(f"Seek to {offset_seconds} on {file_path} did not land (attempt {attempt}); retrying")
            time.sleep(retry_delay)

        self._l.error(
            f"Seek to {offset_seconds} on {file_path} failed to land within {timeout_seconds}s; "
            f"playback may be starting from the wrong position"
        )

    def play_and_wait(self, file_path):
        self._l.info(f"Play and wait on file {file_path}")
        self._close_now_playing()
        self.mpv.vf = ""
        self.mpv.af = ""
        self.mpv.command("playlist-clear")
        self.mpv.command("loadfile", file_path, "replace")
        self.mpv.loop_playlist = "inf"
        self.current_playing_file_path = file_path

        # show NFO overlay if the file has overlay-eligible cached metadata
        try:
            nfo_data = self._nfo_overlay_data(file_path)
            if nfo_data:
                from fs42.nfo_agent import NFOAgent
                self.now_playing_process = NFOAgent.show_overlay(nfo_data)
        except Exception as e:
            self._l.warning(f"Could not start NFO overlay: {e}")

        # this will keep going until channel change or other interrupt
        while True:
            self.tick_osd()
            time.sleep(0.05)
            response = self.input_check_fn()
            if response:
                if self.handle_runtime_command_outcome(response):
                    continue
                self._close_now_playing()
                return response

    def play_file_list(self, file_list):

        try:
            # Filter out any files that don't exist
            valid_files = [f for f in (paths.locate_media(f) for f in file_list) if os.path.exists(f)]

            if not valid_files:
                self._l.error("No valid audio files found in playlist")
                return False
    
            random.shuffle(valid_files)

            self._l.info(f"Starting shuffle playlist with {len(valid_files)} files")

            # Clear any existing playlist and load the files
            self.current_playing_file_path = "playlist"

            self.mpv.command("playlist-clear")

            # Load files using loadfile with 'append' flag
            for i, file_path in enumerate(valid_files):
                self._l.debug(f"Adding to playlist: {file_path}")
                if i == 0:
                    self.mpv.command("loadfile", file_path, "replace")
                else:
                    self.mpv.command("loadfile", file_path, "append")

            self.mpv.loop_playlist = "inf"
            self._l.info("Starting playlist playback")

            return True

        except Exception as e:
            self._l.exception(e)
            self._l.error(f"Error starting playlist: {e}")
            return False

    def _apply_vfx(self, current_time):
        vfx = None
        if "video_scramble_fx" in self.station_config:
            vfx = self.station_config["video_scramble_fx"]
        elif "station_fx" in self.station_config:
            vfx = "station_fx"
            self.scramble_effects["station_fx"] = self.station_config["station_fx"]

        # check if one is set on the slot and override if so
        slot = SlotReader.get_slot(self.station_config, current_time)
        if slot and "video_scramble_fx" in slot:
            if slot["video_scramble_fx"] in self.scramble_effects:
                vfx = slot["video_scramble_fx"]
            else:
                vfx = None

        if vfx:
            if vfx in self.scramble_effects:
                self.mpv.vf = self.scramble_effects[vfx]
                self.skip_reception_check = True
                if vfx == "horizontal_line":
                    self.scrambler = HLScrambledVideoFilter()
                elif vfx == "diagonal_lines":
                    self.scrambler = DiagonalScrambledVideoFilter()
                elif vfx == "color_inversion":
                    self.scrambler = ColorInvertedScrambledVideoFilter()
                elif vfx == "chunky_scramble":
                    self.scrambler = ChunkyScrambledVideoFilter()
            else:
                self._l.warning(f"Scrambler effect '{self.station_config['video_scramble_fx']}' does not exist.")
        else:
            self.skip_reception_check = False
            self.mpv.vf = ""
            self.scrambler = None

        # Apply audio scramble filter if configured
        afx = None
        if "audio_scramble_fx" in self.station_config:
            afx = self.station_config["audio_scramble_fx"]
        if slot and "audio_scramble_fx" in slot:
            afx = slot["audio_scramble_fx"]
        new_afx = self.audio_scramble_effects.get(afx) if afx else None
        if self._active_afx and self._active_afx != new_afx:
            self.mpv.command("af", "remove", self._active_afx)
            self._active_afx = None
        if new_afx and new_afx != self._active_afx:
            self.mpv.command("af", "add", new_afx)
            self._active_afx = new_afx
        elif afx and afx not in self.audio_scramble_effects:
            self._l.warning(f"Audio scramble effect '{afx}' does not exist.")

    def play_image(self, duration):
        pass

    def _guide_alive(self):
        return self.guide_process is not None and self.guide_process.is_alive()

    def _start_guide_process(self, guide_config, hidden):
        self.guide_queue = multiprocessing.Queue()
        self.guide_process = multiprocessing.Process(
            target=_guide_channel_runner(),
            args=(guide_config, self.guide_queue, hidden),
            name="fs42-guide",
        )
        self.guide_process.daemon = True
        self.guide_process.start()

    def warm_guide(self):
        """Start the guide process hidden so tuning to it later is quick."""
        if self._guide_alive():
            return
        guide_config = StationManager().guide_config
        if not guide_config or guide_config.get("network_type") != "guide":
            return
        try:
            self._start_guide_process(guide_config, hidden=True)
            self._l.info("Guide channel process started in the background")
        except Exception as e:
            self._l.warning("Could not pre-start the guide channel: %s", e)

    def _stop_guide_process(self):
        if self.guide_process is None:
            return
        try:
            if self.guide_queue is not None and self.guide_process.is_alive():
                self.guide_queue.put(_guide_commands().exit_process)
                self.guide_process.join(timeout=2)
        except Exception:
            pass
        try:
            if self.guide_process.is_alive():
                self.guide_process.terminate()
                self.guide_process.join(timeout=1)
        except Exception:
            pass
        self.guide_process = None
        self.guide_queue = None

    def show_guide(self, guide_config):
        # Reuse the warm process when there is one; otherwise start it now.
        if self._guide_alive():
            self.guide_queue.put(_guide_commands().show_window)
        else:
            self._start_guide_process(guide_config, hidden=False)
        queue = self.guide_queue
        guide_process = self.guide_process
        self._guide_showing = True
        self._overlay_send("show")

        if "play_sound" in guide_config and guide_config["play_sound"]:
            sound_to_play = guide_config["sound_to_play"]

            # Normalize sound_to_play to always be a list
            if isinstance(sound_to_play, str):
                # Check if it's a directory (with or without trailing slash)
                if os.path.isdir(sound_to_play):
                    # Find all mp3 files in the directory
                    mp3_files = glob.glob(os.path.join(sound_to_play, "*.mp3"))
                    if mp3_files:
                        sound_to_play = sorted(mp3_files)
                        self._l.info(f"Expanded directory {guide_config['sound_to_play']} to {len(sound_to_play)} mp3 files")
                    else:
                        self._l.error(f"Directory {sound_to_play} contains no mp3 files")
                        sound_to_play = []
                else:
                    # Single file - wrap in a list
                    sound_to_play = [sound_to_play]
            elif not isinstance(sound_to_play, list):
                self._l.error(f"Invalid sound_to_play configuration: {type(sound_to_play)}")
                sound_to_play = []

            # Now sound_to_play is always a list
            if len(sound_to_play) == 0:
                # No valid files
                self.mpv.stop()
                self.current_playing_file_path = None
            elif len(sound_to_play) == 1:
                # Single file - use the simple play_file method
                self._l.info(f"Playing guide audio from single file: {sound_to_play[0]}")
                playing = self.play_file(sound_to_play[0])
                if not playing:
                    self.mpv.stop()
                    self.current_playing_file_path = None
            else:
                # Multiple files - use shuffle playlist
                self._l.info(f"Playing guide audio as shuffle playlist with {len(sound_to_play)} files")
                playing = self.play_file_list(sound_to_play)
                if not playing:
                    self.mpv.stop()
                    self.current_playing_file_path = None
        else:
            self.mpv.stop()
            self.current_playing_file_path = None

        # update status
        update_status_socket(
            "playing",
            self.station_config["network_name"],
            self.station_config["channel_number"],
            self.station_config["network_name"],
            timestamp=StationManager().server_conf["date_time_format"],
            content_type="guide",
        )
        keep_going = True
        while keep_going:
            self.tick_osd()
            time.sleep(0.05)
            response = self.input_check_fn()
            if response:
                if self.handle_runtime_command_outcome(response):
                    continue
                self._l.info("Hiding the guide channel")
                self._guide_showing = False
                self._overlay_send("hide")
                if guide_process.is_alive():
                    queue.put(_guide_commands().hide_window)
                else:
                    self._stop_guide_process()
                return response

        return PlayerOutcome(PlayerState.SUCCESS)



    def show_web(self, web_config, blocking=True):
        if not WEB_RENDER_AVAILABLE:
            self._l.error("Web rendering not available - PySide6 not installed")
            msg = "Web rendering requires PySide6 to be installed and configured. Please check documentation."
            return PlayerOutcome(PlayerState.EXIT_COMMAND, msg)

        # create the pipe to communicate with the web channel
        self.web_queue = multiprocessing.Queue()
        self.web_process = multiprocessing.Process(
            target=web_render_runner,
            args=(
                web_config,
                self.web_queue,
            ),
        )
        self.web_process.start()

        # Stop any currently playing content
        self.mpv.stop()
        self.current_playing_file_path = None

        # update status
        update_status_socket(
            "playing",
            self.station_config["network_name"],
            self.station_config["channel_number"],
            self.station_config["network_name"],
            timestamp=StationManager().server_conf["date_time_format"],
            content_type="web",
        )

        if not blocking:
            return PlayerOutcome(PlayerState.SUCCESS)

        # Check if duration is specified for auto-bumps
        duration = web_config.get("duration")
        stop_time = None
        if duration:
            stop_time = datetime.datetime.now() + datetime.timedelta(seconds=duration)
            self._l.info(f"Web content will auto-stop after {duration} seconds")

        keep_going = True
        while keep_going:
            self.tick_osd()
            time.sleep(0.05)

            # Check if duration has expired
            if stop_time and datetime.datetime.now() >= stop_time:
                self._l.info("Web content duration expired, shutting down")
                try:
                    self.web_queue.put("hide_window")
                    self._l.info("Sent hide_window message to web process")
                    self.web_process.join(timeout=3)  # Wait up to 3 seconds
                    if self.web_process.is_alive():
                        self._l.warning("Web process did not terminate gracefully, forcing termination")
                        self.web_process.terminate()
                        self.web_process.join(timeout=1)
                    self._l.info("Web process terminated successfully")
                except Exception as e:
                    self._l.error(f"Error shutting down web process: {e}")
                finally:
                    self.web_process = None
                    self.web_queue = None
                return PlayerOutcome(PlayerState.SUCCESS)

            response = self.input_check_fn()
            if response:
                # Check if this is a web_key command - forward to web process
                if self.handle_runtime_command_outcome(response):
                    continue
                if response.payload and isinstance(response.payload, str) and response.payload.startswith("web_key:"):
                    key_name = response.payload[8:]
                    if self.web_queue:
                        self.web_queue.put(f"key:{key_name}")
                        self._l.info(f"Forwarded key '{key_name}' to web process")
                else:
                    self._l.info("Sending the web channel shutdown command")
                    self.web_queue.put("hide_window")
                    self.web_process.join()
                    self.web_process = None
                    self.web_queue = None
                    return response
        return PlayerOutcome(PlayerState.SUCCESS)

    def schedule_panic(self, network_name):
        self._l.critical("*********************Schedule Panic*********************")
        self._l.critical(f"Schedule not found for {network_name} - attempting to generate a one-day extention")
        if self.schedule_lock:
            self.schedule_lock.acquire()
        try:
            schedule = LiquidSchedule(StationManager().station_by_name(network_name))
            # A schedule that ended in the past is thrown away by the
            # schedule builder; one that ends later today or tomorrow just
            # needs another day.  Either way one call is enough.
            schedule.add_days(1)
            self._l.warning(f"Schedule extended for {network_name} - reloading schedules now")
            LiquidManager().reload_schedules()
        except Exception as e:
            self._l.error(f"Schedule panic failed for {network_name}: {e}")
        finally:
            if self.schedule_lock:
                self.schedule_lock.release()

    def play_slot(self, network_name, when):
        liquid = LiquidManager()

        try:
            play_point = liquid.get_play_point(network_name, when)
            self._current_playing = play_point
        except (ScheduleNotFound, ScheduleQueryNotInBounds):
            self.schedule_panic(network_name)
            self._l.warning(f"Schedules reloaded - retrying play for: {network_name}")
            # fail so we can return and try again
            return PlayerOutcome(PlayerState.FAILED)
        
        if play_point is None:
            self.current_playing_file_path = None
            self.current_playing_block_title = None
            return PlayerOutcome(PlayerState.FAILED)
        
        return self._play_from_point(play_point)

    def _play_from_point(self, play_point: PlayPoint):
        # Fade to black duration before commercial breaks (in seconds)
        FADE_DURATION = 0.5
        fade_active = False

        # Close any existing now playing overlay when starting a new play point
        # This handles channel changes and ensures clean state
        self._close_now_playing()

        if len(play_point.plan):
            initial_skip = play_point.offset

            # iterate over the slice from index to end
            for entry in play_point.plan[play_point.index :]:
                self._l.info(f"Starting entry at {datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
                self._l.info(f"Playing entry {entry}")
                self._l.info(f"Initial Skip: {initial_skip}")
                total_skip = entry.skip + initial_skip

                is_stream = False

                if hasattr(entry, "is_stream"):
                    is_stream = entry.is_stream

                title = play_point.block_title
                content_type = getattr(entry, 'content_type', 'feature')  # Get content_type from entry, default to 'feature'
                media_type = getattr(entry, 'media_type', 'video')  # Get media_type from entry, default to 'video'
                worked = self.play_file(entry.path, file_duration=entry.duration, offset_seconds=total_skip, is_stream=is_stream, title=title, content_type=content_type, media_type=media_type)
                if self._pending_response:
                    response = self._pending_response
                    self._pending_response = None
                    return response
                stream_is_down = False
                last_osd_refresh = 0.0
                if not worked:
                    if is_stream:
                        self._l.warning(f"Stream unavailable, activating fallback: {entry.path}")
                        stream_is_down = True
                        last_osd_refresh = time.time()
                        self._show_stream_down()
                    else:
                        return PlayerOutcome(PlayerState.FAILED)
                # Seek now happens inside play_file() before overlay is shown

                # Detect if this video is being clipped (stopping before natural end)
                is_clipped = False
                try:
                    actual_file_duration = self.mpv.duration
                    stop_position = total_skip + (entry.duration - initial_skip)
                    # If stopping before end (with tolerance), we're clipping
                    is_clipped = stop_position < (actual_file_duration - 0.5)
                    self._l.info(f"File duration: {actual_file_duration:.2f}s, stop at: {stop_position:.2f}s, clipped: {is_clipped}")
                except Exception as e:
                    self._l.info(f"Could not determine if clipped: {e}")
                    is_clipped = False

                if entry.duration:
                    self._l.info(f"Monitoring for: {entry.duration - initial_skip}")

                    # Calculate target end time using wall clock
                    target_end_time = datetime.datetime.now() + datetime.timedelta(seconds=(entry.duration - initial_skip))
                    self._l.info(f"Target end time: {target_end_time.strftime('%H:%M:%S.%f')[:-3]}")
                    stream_down_message = (self.station_config.get("stream_down_message", "TECHNICAL DIFFICULTIES") if self.station_config else "TECHNICAL DIFFICULTIES")

                    # this is our main event loop
                    keep_waiting = True
                    while keep_waiting:
                        if not self.skip_reception_check:
                            self.update_reception()
                        else:
                            if self.scrambler:
                                self.mpv.vf = self.scrambler.update_filter()

                        # Calculate time remaining based on wall clock
                        time_remaining = (target_end_time - datetime.datetime.now()).total_seconds()

                        # Initiate fade-to-black effect when entering fade window (only if clipped)
                        if 0 < time_remaining <= FADE_DURATION and not fade_active and is_clipped:
                            self._l.info(f"Starting fade with {time_remaining:.2f}s remaining")

                            # Use MPV's built-in fade filter with duration
                            # Fade video to black (only if not using scramble effects)
                            if not self.skip_reception_check:
                                try:
                                    # Use fade filter: fade out to black over remaining time
                                    self.mpv.vf = f"fade=t=out:st=0:d={time_remaining}"
                                except Exception as e:
                                    self._l.debug(f"Could not set video fade filter: {e}")

                            fade_active = True

                        # Detect stream drop mid-playback and show fallback
                        if is_stream and not stream_is_down:
                            try:
                                if self.mpv.time_pos is None:
                                    self._l.warning(f"Stream dropped mid-playback: {entry.path}")
                                    stream_is_down = True
                                    last_osd_refresh = 0.0
                                    self._show_stream_down()
                            except Exception:
                                pass
                        elif is_stream and stream_is_down:
                            _now = time.time()
                            if _now - last_osd_refresh >= 8.0:
                                try:
                                    self.mpv.command("show-text", stream_down_message, 10000)
                                except Exception:
                                    pass
                                last_osd_refresh = _now

                        if time_remaining <= 0:
                            if self.web_process:
                                try:
                                    self.web_queue.put("hide_window")
                                    self.web_process.join(timeout=3)
                                    if self.web_process.is_alive():
                                        self._l.warning("Web process did not terminate gracefully, forcing termination")
                                        self.web_process.terminate()
                                        self.web_process.join(timeout=1)
                                except Exception as e:
                                    self._l.error(f"Error shutting down web process: {e}")
                                finally:
                                    self.web_process = None
                                    self.web_queue = None
                            keep_waiting = False
                        else:
                            # debounce time
                            self.tick_osd()
                            time.sleep(0.05)
                            response = self.input_check_fn()
                            if response:
                                if self.handle_runtime_command_outcome(response):
                                    continue
                                if response.status == PlayerState.CHANNEL_CHANGE:
                                    if stream_is_down:
                                        try:
                                            self.mpv.command("show-text", "", 1)
                                        except Exception:
                                            pass
                                    if self.web_process:
                                        try:
                                            self.web_queue.put("hide_window")
                                            self.web_process.join(timeout=2)
                                            if self.web_process.is_alive():
                                                self._l.warning("Web process did not terminate gracefully on channel change, forcing termination")
                                                self.web_process.terminate()
                                                self.web_process.join(timeout=1)
                                        except Exception as e:
                                            self._l.error(f"Error shutting down web process on channel change: {e}")
                                        finally:
                                            self.web_process = None
                                            self.web_queue = None
                                return response
                else:
                    return PlayerOutcome(PlayerState.FAILED)

                # Log timing for debugging inter-entry delays
                self._l.info(f"Segment ended at {datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}")

                # Reset fade state for next segment
                fade_active = False

                initial_skip = 0

            self._l.info("Done playing block")
            return PlayerOutcome(PlayerState.SUCCESS)
        else:
            self.current_playing_file_path = None
            return PlayerOutcome(PlayerState.FAILED, "Failure getting index...")

    def get_current_path(self):
        if self.current_playing_file_path:
            basename = os.path.basename(self.current_playing_file_path)
            title, _ = os.path.splitext(basename)
            return title
        return None
