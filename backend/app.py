import atexit
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

import requests
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

# Cache health check results
health_cache: Dict[str, dict] = {}


def load_apps() -> List[dict]:
    with open(APPS_PATH) as f:
        return json.load(f)


APPS = load_apps()


def get_app(app_id: str) -> Optional[dict]:
    for a in APPS:
        if a.get("id") == app_id:
            return a
    return None


def is_port_in_use(port: int) -> bool:
    """Check if a port is in use (app running externally)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("localhost", port))
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False


def check_app_health(app_cfg: dict) -> dict:
    """Check health of an app by calling its health endpoint."""
    app_id = app_cfg.get("id")
    health_endpoint = app_cfg.get("health_endpoint")
    port = app_cfg.get("port")

    result = {
        "healthy": False,
        "response": None,
        "error": None,
        "checked_at": time.time()
    }

    if not health_endpoint:
        # No health endpoint configured, can only check if process is running
        result["error"] = "No health endpoint configured"
        return result

    try:
        url = f"http://localhost:{port}{health_endpoint}"
        resp = requests.get(url, timeout=2)
        result["healthy"] = resp.status_code == 200
        try:
            result["response"] = resp.json()
        except Exception:
            result["response"] = resp.text[:100]
    except requests.exceptions.ConnectionError:
        result["error"] = "Connection refused"
    except requests.exceptions.Timeout:
        result["error"] = "Timeout"
    except Exception as e:
        result["error"] = str(e)

    health_cache[app_id] = result
    return result


def format_status() -> List[dict]:
    status = []
    for app_cfg in APPS:
        app_id = app_cfg.get("id")
        port = app_cfg.get("port")
        proc = running_processes.get(app_id)
        is_managed = proc is not None and proc.poll() is None

        # Check if app is running (either managed by us or externally)
        is_running = is_managed or (port and is_port_in_use(port))

        # Get cached health or check now if process is running
        health_info = health_cache.get(app_id, {})

        status.append(
            {
                "id": app_id,
                "name": app_cfg.get("name"),
                "url": app_cfg.get("url"),
                "web_url": app_cfg.get("web_url") or app_cfg.get("url"),
                "port": app_cfg.get("port"),
                "running": is_running,
                "pid": proc.pid if proc and proc.poll() is None else None,
                "health_endpoint": app_cfg.get("health_endpoint"),
                "healthy": health_info.get("healthy", False) if is_running else False,
                "health_error": health_info.get("error"),
            }
        )
    return status


def start_subprocess(app_cfg: dict) -> subprocess.Popen:
    """Start an app subprocess based on its launch_type."""
    launch_type = app_cfg.get("launch_type", "python")
    script = app_cfg["script"]
    venv = app_cfg.get("venv")

    # Support both absolute and relative paths
    if os.path.isabs(script):
        script_path = script
    else:
        script_path = os.path.abspath(os.path.join(BASE_DIR, script))

    # Determine which Python executable to use
    if venv:
        python_exe = os.path.join(venv, "bin", "python")
        if not os.path.exists(python_exe):
            python_exe = sys.executable  # Fallback to system Python
    else:
        python_exe = sys.executable

    creationflags = 0
    preexec_fn = None

    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        if venv:
            python_exe = os.path.join(venv, "Scripts", "python.exe")
    else:
        preexec_fn = os.setsid

    # Create log files for app output
    app_id = app_cfg.get("id", "unknown")
    log_dir = "/tmp"
    stdout_log = open(os.path.join(log_dir, f"flightdeck-{app_id}.log"), "a")
    stderr_log = open(os.path.join(log_dir, f"flightdeck-{app_id}.error.log"), "a")

    if launch_type == "uvicorn":
        # For uvicorn apps, script_path is the working directory
        uvicorn_app = app_cfg.get("uvicorn_app")
        port = app_cfg.get("port", 8000)

        return subprocess.Popen(
            [python_exe, "-m", "uvicorn", uvicorn_app,
             "--host", "0.0.0.0", "--port", str(port)],
            cwd=script_path,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
            stdout=stdout_log,
            stderr=stderr_log,
        )
    else:
        # Standard python script execution
        # Support additional launch arguments
        launch_args = app_cfg.get("launch_args", [])
        cmd = [python_exe, script_path] + launch_args

        return subprocess.Popen(
            cmd,
            cwd=os.path.dirname(script_path) or None,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
            stdout=stdout_log,
            stderr=stderr_log,
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


@app.route("/api/health/<app_id>", methods=["GET"])
def get_app_health(app_id):
    """Get health status for a specific app."""
    app_cfg = get_app(app_id)
    if not app_cfg:
        return jsonify({"error": "App not found"}), 404

    health = check_app_health(app_cfg)
    return jsonify(health)


@app.route("/api/health", methods=["GET"])
def get_all_health():
    """Check health of all running apps."""
    results = {}
    for app_cfg in APPS:
        app_id = app_cfg.get("id")
        proc = running_processes.get(app_id)
        if proc and proc.poll() is None:
            results[app_id] = check_app_health(app_cfg)
        else:
            results[app_id] = {"healthy": False, "error": "Not running"}
    return jsonify(results)


def background_health_checker():
    """Background thread that periodically checks health of running apps."""
    while True:
        time.sleep(10)  # Check every 10 seconds
        for app_cfg in APPS:
            app_id = app_cfg.get("id")
            port = app_cfg.get("port")
            proc = running_processes.get(app_id)
            is_managed = proc and proc.poll() is None
            # Check health if app is running (managed or external)
            if is_managed or (port and is_port_in_use(port)):
                check_app_health(app_cfg)
        # Emit updated status to all connected clients
        try:
            socketio.emit("status_update", {"apps": format_status()})
        except Exception:
            pass


# Start background health checker thread
health_thread = threading.Thread(target=background_health_checker, daemon=True)
health_thread.start()


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5050)
