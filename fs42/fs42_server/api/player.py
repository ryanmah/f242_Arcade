from fastapi import APIRouter, Request, HTTPException
import json
import re
import platform
import shutil
import subprocess

from fs42 import ipc
from fs42 import platform_compat
from fs42.station_manager import StationManager

router = APIRouter(prefix="/player", tags=["player"])

@router.get("/info")
async def get_info():
    """Get system information including CPU temp, memory usage, and CPU usage"""
    info = {}
    
    # Get CPU temperature
    temp_info = await _get_cpu_temperature()
    info.update(temp_info)
    
    # Get memory information
    memory_info = await _get_memory_info()
    info.update(memory_info)
    
    # Get CPU usage
    cpu_info = await _get_cpu_info()
    info.update(cpu_info)
    
    # Get system information
    try:
        info["system"] = {
            "platform": platform.system(),
            "architecture": platform.machine(),
            "hostname": platform.node()
        }
    except Exception:
        info["system"] = {"error": "unavailable"}
    
    return info


async def _get_cpu_temperature():
    """Get CPU temperature using various methods depending on the system"""
    
    # Method 1: Raspberry Pi vcgencmd (original method)
    if shutil.which("vcgencmd"):
        try:
            result = platform_compat.run_hidden(["vcgencmd", "measure_temp"]).stdout
            # output in form: temp=49.4'C
            temp_c = float(result.split("=")[1].split("'")[0])
            temp_f = round((temp_c * 1.8) + 32)
            return {
                "temperature_c": round(temp_c, 1),
                "temperature_f": temp_f,
                "temp_source": "vcgencmd"
            }
        except Exception:
            pass
    
    # Method 2: Linux thermal zones (most modern Linux systems)
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp_millicelsius = int(f.read().strip())
            temp_c = temp_millicelsius / 1000.0
            temp_f = round((temp_c * 1.8) + 32)
            return {
                "temperature_c": round(temp_c, 1),
                "temperature_f": temp_f,
                "temp_source": "thermal_zone"
            }
    except Exception:
        pass
    
    # Method 3: psutil (Linux/FreeBSD; returns nothing on Windows)
    try:
        import psutil

        readings = psutil.sensors_temperatures() or {}
        for entries in readings.values():
            for entry in entries:
                if entry.current:
                    temp_c = float(entry.current)
                    return {
                        "temperature_c": round(temp_c, 1),
                        "temperature_f": round((temp_c * 1.8) + 32),
                        "temp_source": "psutil",
                    }
    except Exception:
        pass

    # Method 4: Using lm-sensors (if available)
    if shutil.which("sensors"):
        try:
            result = platform_compat.run_hidden(["sensors"]).stdout
            # Look for CPU temperature in sensors output
            for line in result.split('\n'):
                if 'Core 0' in line or 'CPU' in line or 'temp1' in line:
                    match = re.search(r'([+-]?\d+\.?\d*)\s*°C', line)
                    if match:
                        temp_c = float(match.group(1))
                        temp_f = round((temp_c * 1.8) + 32)
                        return {
                            "temperature_c": round(temp_c, 1),
                            "temperature_f": temp_f,
                            "temp_source": "sensors"
                        }
        except Exception:
            pass
    
    return {"temperature": "unavailable"}


async def _get_memory_info():
    """Memory usage, via psutil so it works off Linux too."""
    try:
        import psutil

        virtual = psutil.virtual_memory()
        return {
            "memory": {
                "total_gb": round(virtual.total / (1024**3), 1),
                "available_gb": round(virtual.available / (1024**3), 1),
                "used_gb": round((virtual.total - virtual.available) / (1024**3), 1),
                "used_percent": round(virtual.percent, 1),
            }
        }
    except Exception:
        return {"memory": {"error": "unavailable"}}


async def _get_cpu_info():
    """CPU core count and load, via psutil so it works off Linux too."""
    try:
        import psutil

        cpu_count = psutil.cpu_count(logical=True) or 1
        cpu = {"cores": cpu_count, "used_percent": psutil.cpu_percent(interval=None)}
        try:
            load_1min, load_5min, load_15min = psutil.getloadavg()
            cpu.update(
                {
                    "load_1min": round(load_1min, 2),
                    "load_5min": round(load_5min, 2),
                    "load_15min": round(load_15min, 2),
                    "load_percent": round((load_1min / cpu_count) * 100, 1),
                }
            )
        except (AttributeError, OSError):
            # Load average is meaningless on Windows; cpu_percent covers it.
            cpu["load"] = "unavailable"
        return {"cpu": cpu}
    except Exception:
        return {"cpu": {"error": "unavailable"}}


@router.get("/status")
async def get_player_status():
    status = ipc.get_status()
    if not status:
        return {"status": "stopped", "network_name": "", "channel_number": -1}
    return status


@router.get("/status/queue_connected")
async def get_connected(request: Request):
    # Either transport counts: the in-process queue, or the state bus that
    # the supervisor-managed player drains.
    command_queue = request.app.state.player_command_queue
    return {"queue_connected": bool(command_queue) or ipc.pending_count() >= 0}

@router.get("/channels/{channel}")
async def player_channel(channel: str):
    command = {"command": "direct", "channel": -1}
    if channel.isnumeric():
        command["channel"] = int(channel)
    elif channel == "up":
        command["command"] = "up"
    elif channel == "down":
        command["command"] = "down"
    else:
        return {"error": "Invalid channel command. Use a number, 'up', or 'down'."}

    ipc.push(ipc.TOPIC_CHANNEL, command)
    return {"command": command}


@router.post("/menu/open")
async def menu_open(request: Request):
    """Open the on-screen channel menu (what Escape does on the keyboard)."""
    send_player_command(request, {"command": "menu"})
    return {"status": "ok"}


@router.post("/menu/input/{action}")
async def menu_input(action: str):
    """Drive the on-screen menu: up, down, left, right, select, back, digit_N."""
    from fs42.menu.input import is_action

    if not is_action(action):
        raise HTTPException(status_code=400, detail=f"Unknown menu action: {action}")
    ipc.push(ipc.TOPIC_MENU_INPUT, {"action": action})
    return {"status": "ok", "action": action}


@router.get("/menu")
async def menu_state():
    """Whether the menu is open, and what it is showing (for the remote)."""
    is_open = bool(ipc.get_state(ipc.KEY_MENU_OPEN))
    return {"open": is_open, "page": ipc.get_state(ipc.KEY_MENU_PAGE) if is_open else None}


@router.post("/channels/guide")
async def show_guide(request:Request):
    send_player_command(request, {"command": "guide"})
    return {"status" : "ok"}

@router.get("/commands/stop")
@router.post("/commands/stop")
async def player_stop(request: Request):
    send_player_command(request, {"command": "exit"})
    ipc.request_shutdown("api")
    return {"status": "stopped"}


def send_player_command(request: Request, payload: dict):
    """Deliver a command to the player process.

    Two transports, same payload: a multiprocessing queue when the player
    started this server itself, and the sqlite state bus when the supervisor
    runs us as an independent process.
    """
    command_queue = getattr(request.app.state, "player_command_queue", None)
    if command_queue:
        command_queue.put(payload)
        return True
    if ipc.push(ipc.TOPIC_PLAYER_CMD, payload):
        return True
    raise HTTPException(status_code=503, detail="Player is not reachable.")


async def _queue_mpv_command(request: Request, action: str):
    send_player_command(request, {"command": "mpv_command", "action": action})
    return {"status": "ok", "command": "mpv_command", "action": action}


@router.get("/mpv/toggle-subtitles")
@router.post("/mpv/toggle-subtitles")
async def mpv_toggle_subtitles(request: Request):
    """Toggle subtitle visibility for the active mpv player."""
    return await _queue_mpv_command(request, "toggle_subtitles")


@router.get("/mpv/cycle-subtitles")
@router.post("/mpv/cycle-subtitles")
async def mpv_cycle_subtitles(request: Request):
    """Cycle through available subtitle tracks for the active mpv player."""
    return await _queue_mpv_command(request, "cycle_subtitles")


@router.get("/mpv/cycle-audio")
@router.post("/mpv/cycle-audio")
async def mpv_cycle_audio(request: Request):
    """Cycle through available audio tracks for the active mpv player."""
    return await _queue_mpv_command(request, "cycle_audio")


@router.post("/ticker")
async def show_ticker(request: Request):
    data = await request.json()
    send_player_command(request, {
        "command": "ticker",
        "message": data.get("message", ""),
        "header": data.get("header", "FS42"),
        "style": data.get("style", "fieldstation"),
        "iterations": data.get("iterations", 2),
    })
    return {"status": "success"}

@router.get("/volume/up")
@router.post("/volume/up")
async def volume_up(request: Request):
    """Increase playback volume."""
    return _volume_command(request, "up")


@router.get("/volume/down")
@router.post("/volume/down")
async def volume_down(request: Request):
    """Decrease playback volume."""
    return _volume_command(request, "down")


@router.get("/volume/mute")
@router.post("/volume/mute")
async def volume_mute(request: Request):
    """Toggle mute."""
    return _volume_command(request, "mute")


@router.get("/volume")
async def volume_state():
    """Last known volume, as published by the player."""
    state = ipc.get_state(ipc.KEY_VOLUME)
    if not state:
        return {"volume": "unknown", "muted": False, "method": "mpv"}
    return state


def _volume_command(request: Request, action: str):
    """Adjust volume through mpv rather than the system mixer.

    Upstream shelled out to amixer, pactl or wpctl and returned HTTP 500 when
    none of them existed - which is every Windows machine.  mpv owns the audio
    stream we care about, already has a control channel, and scoping volume to
    the TV instead of the whole desktop is the correct behaviour for an
    appliance anyway.
    """
    send_player_command(request, {"command": "mpv_command", "action": f"volume_{action}"})
    return {"status": "ok", "command": "volume", "action": action}
