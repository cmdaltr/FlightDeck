import atexit
import json
import os
import signal
import subprocess
import sys
from typing import Dict, List, Optional

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APPS_PATH = os.path.join(BASE_DIR, "apps.json")

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# Track running processes by app id
running_processes: Dict[str, subprocess.Popen] = {}


def load_apps() -> List[dict]:
    with open(APPS_PATH) as f:
        return json.load(f)


APPS = load_apps()


def get_app(app_id: str) -> Optional[dict]:
    for app in APPS:
        if app.get("id") == app_id:
            return app
    return None


def format_status() -> List[dict]:
    status = []
    for app_cfg in APPS:
        app_id = app_cfg.get("id")
        proc = running_processes.get(app_id)
        status.append(
            {
                "id": app_id,
                "name": app_cfg.get("name"),
                "url": app_cfg.get("url"),
                "running": proc is not None and proc.poll() is None,
                "pid": proc.pid if proc and proc.poll() is None else None,
            }
        )
    return status


def start_subprocess(app_cfg: dict) -> subprocess.Popen:
    script_path = os.path.abspath(os.path.join(BASE_DIR, app_cfg["script"]))
    creationflags = 0
    preexec_fn = None

    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        preexec_fn = os.setsid

    return subprocess.Popen(
        [sys.executable, script_path],
        cwd=os.path.dirname(script_path) or None,
        creationflags=creationflags,
        preexec_fn=preexec_fn,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def stop_process_group(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:
                proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        proc.terminate()


def cleanup_processes(*_args) -> None:
    for app_id, proc in list(running_processes.items()):
        if proc.poll() is None:
            stop_process_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        running_processes.pop(app_id, None)


atexit.register(cleanup_processes)
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, cleanup_processes)


@app.route("/api/apps", methods=["GET"])
def list_apps():
    return jsonify(format_status())


@app.route("/api/start/<app_id>", methods=["POST"])
def start_app(app_id):
    app_cfg = get_app(app_id)
    if not app_cfg:
        return jsonify({"error": "App not found"}), 404

    existing = running_processes.get(app_id)
    if existing and existing.poll() is None:
        return jsonify({"message": "Already running", "status": format_status()}), 200

    proc = start_subprocess(app_cfg)
    running_processes[app_id] = proc
    statuses = format_status()
    socketio.emit("status_update", {"apps": statuses})
    return jsonify({"message": "Started", "status": statuses}), 200


@app.route("/api/stop/<app_id>", methods=["POST"])
def stop_app(app_id):
    proc = running_processes.get(app_id)
    if not proc or proc.poll() is not None:
        running_processes.pop(app_id, None)
        return jsonify({"message": "Already stopped", "status": format_status()}), 200

    stop_process_group(proc)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    running_processes.pop(app_id, None)
    statuses = format_status()
    socketio.emit("status_update", {"apps": statuses})
    return jsonify({"message": "Stopped", "status": statuses}), 200


@socketio.on("connect")
def on_connect():
    emit("status_update", {"apps": format_status()})


@socketio.on("request_status")
def on_request_status():
    emit("status_update", {"apps": format_status()})


@app.route("/")
def healthcheck():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
