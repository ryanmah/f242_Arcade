"""Application settings that live outside station configs.

Today that is the data folder: where confs/, catalog/ and runtime/ live.
Pointing it at an external drive that already holds a FieldStation42 layout
is the whole use case.  The choice is written to a launcher file in the
default location and takes effect when the supervisor restarts everything,
which the page offers to do.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fs42 import ipc, paths

router = APIRouter(prefix="/settings")
_l = logging.getLogger("SETTINGS")


class DataRootRequest(BaseModel):
    path: str


def _state():
    info = paths.data_root_info()
    info["current_contents"] = paths.inspect_data_root(info["current"])
    if info["configured"]:
        info["configured_contents"] = paths.inspect_data_root(info["configured"])
    return info


@router.get("/data_root")
async def get_data_root():
    return _state()


@router.post("/data_root/check")
async def check_data_root(request: DataRootRequest):
    path = request.path.strip().strip('"')
    if not path:
        raise HTTPException(status_code=400, detail="No folder given")
    return paths.inspect_data_root(path)


@router.post("/data_root")
async def set_data_root(request: DataRootRequest):
    info = paths.data_root_info()
    if info["locked_by_environment"]:
        raise HTTPException(
            status_code=409,
            detail="The data folder is set by the FS42_HOME environment variable and cannot be changed here.",
        )
    path = request.path.strip().strip('"')
    if not path:
        raise HTTPException(status_code=400, detail="No folder given")
    inspection = paths.inspect_data_root(path)
    if not inspection["is_dir"]:
        raise HTTPException(status_code=400, detail=inspection["problems"][0] if inspection["problems"] else "Not a folder")
    if not inspection["writable"]:
        raise HTTPException(status_code=400, detail="FieldStation42 cannot write to that folder.")
    paths.set_data_root(path)
    _l.info("Data folder set to %s (takes effect on restart)", path)
    return _state()


@router.post("/data_root/reset")
async def reset_data_root():
    info = paths.data_root_info()
    if info["locked_by_environment"]:
        raise HTTPException(status_code=409, detail="The data folder is set by the FS42_HOME environment variable.")
    paths.set_data_root(None)
    _l.info("Data folder reset to the default")
    return _state()


@router.post("/restart")
async def restart():
    """Ask the supervisor to stop and start every component.

    Only meaningful when running under the supervisor (the normal case);
    the web console polls until the server answers again.
    """
    ipc.request_restart("settings")
    return {"status": "restarting"}


@router.get("/ping")
async def ping():
    return {"status": "ok", "version": paths.app_version(), "data_root": str(paths.data())}


# ------------------------------------------------------------------ updates

@router.get("/update/check")
async def update_check():
    """Compare this copy with the latest GitHub release (or branch)."""
    import asyncio

    from fs42 import updater

    result = await asyncio.to_thread(updater.check)
    result["job"] = updater.job().status()
    return result


@router.post("/update/install")
async def update_install():
    """Download and run the update in the background; poll /update/status."""
    from fs42 import updater

    return updater.job().start()


@router.get("/update/status")
async def update_status():
    from fs42 import updater

    return {"version": paths.app_version(), **updater.job().status()}
