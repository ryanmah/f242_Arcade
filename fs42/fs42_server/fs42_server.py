import asyncio
import logging
import os

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import FileResponse

from fs42 import ipc
from fs42 import paths
from fs42.fs42_server.overlay_static import OverlayStaticFiles
from fs42.station_manager import StationManager
from .api import routers

_shutdown_queue = None
player_command_queue = None


@asynccontextmanager
async def _lifespan(app):
    async def shutdown_monitor():
        while True:
            await asyncio.sleep(1)
            if _shutdown_queue is not None:
                try:
                    if _shutdown_queue.get_nowait() == "shutdown":
                        os._exit(0)
                except Exception:
                    pass
            # The supervisor runs this process independently of the player, so
            # the shared state bus is the channel that always exists.
            try:
                if ipc.shutdown_requested():
                    os._exit(0)
            except Exception:
                pass

    asyncio.get_event_loop().create_task(shutdown_monitor())
    yield


# Create FastAPI app
fapi = FastAPI(title="FieldStation42 API", lifespan=_lifespan)


@fapi.get("/")
async def root():
    return FileResponse(str(paths.static_dir() / "index.html"))


@fapi.get("/remote")
async def remote():
    return FileResponse(str(paths.static_dir() / "remote.html"))


@fapi.get('/favicon.ico', include_in_schema=False)
async def favicon():
    return FileResponse(str(paths.static_dir() / "favicon.ico"))


# Include routers from the api package
for router in routers:
    fapi.include_router(router)


def _install_log_filter():
    class PlayerStatusFilter(logging.Filter):
        def filter(self, record):
            return '/player/status' not in record.getMessage()

    logging.getLogger("uvicorn.access").addFilter(PlayerStatusFilter())


def _mount_static():
    """Serve bundled assets with user-supplied ones layered on top.

    When frozen, the static tree lives inside a read-only bundle, but users
    legitimately add themes, bump videos and PPV art.  Those live in the data
    directory and shadow bundled files of the same name.
    """
    guide_videos = paths.runtime("guide_videos")
    guide_videos.mkdir(parents=True, exist_ok=True)
    paths.static_overlay().mkdir(parents=True, exist_ok=True)

    fapi.mount(
        "/static",
        OverlayStaticFiles(
            directories=[paths.static_overlay(), paths.static_dir()], html=True
        ),
        name="static",
    )
    fapi.mount(
        "/guide_videos",
        OverlayStaticFiles(directories=[guide_videos]),
        name="guide_videos",
    )


def _serve():
    conf = StationManager().server_conf
    # log_config=None: uvicorn's default config builds a colour formatter
    # that probes sys.stdout.isatty(), which the windowed Windows build does
    # not have.  Our own logging setup (cli._configure_logging) applies.
    uvicorn.run(fapi, host=conf["server_host"], port=conf["server_port"], log_config=None)


def run_with_shutdown_queue(shutdown_queue, command_queue):
    """Entry point when the player owns the API as a child process."""
    global player_command_queue, _shutdown_queue
    _install_log_filter()
    player_command_queue = command_queue
    _shutdown_queue = shutdown_queue
    fapi.state.player_command_queue = command_queue
    _mount_static()
    _serve()


def run_standalone():
    """Entry point when the supervisor runs the API as its own process.

    There is no shared multiprocessing queue here; player commands travel over
    the sqlite state bus instead, so the endpoints behave identically.
    """
    _install_log_filter()
    fapi.state.player_command_queue = None
    _mount_static()
    _serve()


def mount_fs42_api():
    """Backwards-compatible name used by station_42.py."""
    run_standalone()


if __name__ == "__main__":
    run_standalone()
