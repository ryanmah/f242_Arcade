import threading
import traceback
import uuid
from fastapi import APIRouter, Request

from fs42 import build_jobs
from fs42.fs42_server.api.player import send_player_command
from fs42.liquid_manager import LiquidManager

router = APIRouter(prefix="/build", tags=["build"])

# Global dicts and locks for task tracking
rebuild_tasks = {}
rebuild_tasks_lock = threading.Lock()
add_time_tasks = {}
add_time_tasks_lock = threading.Lock()


def _run_job(tasks, lock, task_id, request, job):
    """Run one build_jobs callable on a thread, logging into a task dict.

    The job bodies themselves live in fs42/build_jobs.py so the in-app menu
    can run the same work with its own progress sink.
    """

    def log(line):
        with lock:
            tasks[task_id]["log"] += f"{line}\n"

    def worker():
        try:
            with lock:
                tasks[task_id]["status"] = "running"
            job(log)
            with lock:
                tasks[task_id]["status"] = "done"
            log("Reloading data and state.")
            # Reaches the player over whichever transport is live; the local
            # reload is only for a console running with no player.
            try:
                send_player_command(request, {"command": "reload_data"})
            except Exception:
                LiquidManager().reload_schedules()
        except Exception as e:
            with lock:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["log"] += f"Error: {e}\n\nDetailed Error Message:\n{traceback.format_exc()}"

    threading.Thread(target=worker, daemon=True).start()


def _new_task(tasks, lock):
    task_id = str(uuid.uuid4())
    with lock:
        tasks[task_id] = {"status": "starting", "log": ""}
    return task_id


def _task_status(tasks, lock, task_id):
    with lock:
        task = tasks.get(task_id)
        if not task:
            return {"error": "Task ID not found."}
        return {"status": task["status"], "log": task["log"]}


@router.post("/catalog/{network_name}")
async def rebuild_catalog(network_name: str, request: Request):
    task_id = _new_task(rebuild_tasks, rebuild_tasks_lock)
    _run_job(rebuild_tasks, rebuild_tasks_lock, task_id, request,
             lambda log: build_jobs.rebuild_catalog(network_name, log))
    return {"task_id": task_id}


@router.get("/catalog/status/{task_id}")
async def rebuild_catalog_status(task_id: str):
    return _task_status(rebuild_tasks, rebuild_tasks_lock, task_id)


@router.post("/schedule/add_time/{amount}/{network_name}")
async def add_time_to_schedule(amount: str, network_name: str, request: Request):
    task_id = _new_task(add_time_tasks, add_time_tasks_lock)
    _run_job(add_time_tasks, add_time_tasks_lock, task_id, request,
             lambda log: build_jobs.add_schedule_time(amount, network_name, log))
    return {"task_id": task_id}


@router.get("/schedule/add_time/status/{task_id}")
async def add_time_to_schedule_status(task_id: str):
    return _task_status(add_time_tasks, add_time_tasks_lock, task_id)


@router.post("/schedule/reset/{network_name}")
async def rebuild_schedule(network_name: str, request: Request):
    task_id = _new_task(rebuild_tasks, rebuild_tasks_lock)
    _run_job(rebuild_tasks, rebuild_tasks_lock, task_id, request,
             lambda log: build_jobs.reset_schedule(network_name, log))
    return {"task_id": task_id}


@router.get("/schedule/reset/status/{task_id}")
async def rebuild_schedule_status(task_id: str):
    return _task_status(rebuild_tasks, rebuild_tasks_lock, task_id)
