import atexit
import json
import os
import re
import shutil
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
VENV_CACHE_DIR = os.path.expanduser("~/.local/share/flightdeck-venvs")

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


BACKUP_DIR = os.path.join(BASE_DIR, "backups")


def save_apps(apps_list: List[dict]) -> None:
    # Timestamped backup before every write; keep the 10 most recent
    if os.path.isfile(APPS_PATH):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        shutil.copy2(APPS_PATH, os.path.join(BACKUP_DIR, f"apps.{ts}.json"))
        kept = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("apps.") and f.endswith(".json"))
        for old in kept[:-10]:
            os.remove(os.path.join(BACKUP_DIR, old))

    with open(APPS_PATH, "w") as f:
        json.dump(apps_list, f, indent=2)
    global APPS
    APPS = apps_list


APPS = load_apps()


def get_app(app_id: str) -> Optional[dict]:
    for a in APPS:
        if a.get("id") == app_id:
            return a
    return None


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("localhost", port))
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False


def check_app_health(app_cfg: dict) -> dict:
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
        is_running = is_managed or (port and is_port_in_use(port))
        health_info = health_cache.get(app_id, {})

        status.append({
            "id": app_id,
            "name": app_cfg.get("name"),
            "script": app_cfg.get("script"),
            "url": app_cfg.get("url"),
            "web_url": app_cfg.get("web_url") or app_cfg.get("url"),
            "port": app_cfg.get("port"),
            "launch_type": app_cfg.get("launch_type", "python"),
            "autostart": app_cfg.get("autostart", False),
            "running": is_running,
            "pid": proc.pid if proc and proc.poll() is None else None,
            "health_endpoint": app_cfg.get("health_endpoint"),
            "healthy": health_info.get("healthy", False) if is_running else False,
            "health_error": health_info.get("error"),
        })
    return status


def _is_valid_python_binary(path: str) -> bool:
    """Return True if path is an actual executable binary, not an OneDrive-mangled symlink text file."""
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    try:
        with open(path, "rb") as f:
            hdr = f.read(4)
        return hdr in (
            b"\x7fELF",           # ELF (Linux)
            b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit LE
            b"\xce\xfa\xed\xfe",  # Mach-O 32-bit LE
            b"\xfe\xed\xfa\xcf",  # Mach-O 64-bit BE
            b"\xfe\xed\xfa\xce",  # Mach-O 32-bit BE
            b"\xca\xfe\xba\xbe",  # Mach-O fat binary
        )
    except OSError:
        return False


def _find_requirements(app_cfg: dict) -> Optional[str]:
    """Walk up from the app's script directory looking for requirements.txt."""
    script = app_cfg.get("script")
    if not script:
        return None
    base = script if os.path.isdir(script) else os.path.dirname(script)
    current = os.path.abspath(base)
    for _ in range(3):
        req = os.path.join(current, "requirements.txt")
        if os.path.isfile(req):
            return req
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _start_docker_deps(app_cfg: dict) -> Optional[str]:
    """Start Docker service dependencies listed in app_cfg['docker_deps'].
    Returns an error string on failure, None on success."""
    deps = app_cfg.get("docker_deps")
    if not deps:
        return None
    compose_dir = deps.get("compose_dir", "")
    services = deps.get("services", [])
    if not compose_dir or not services:
        return None
    rc, out = _docker_compose(compose_dir, "up", "-d", "--remove-orphans", *services)
    if rc != 0:
        return out
    return None


def get_docker_compose_dir(app_cfg: dict) -> Optional[str]:
    """Return the directory containing docker-compose.yml for a docker app."""
    script = app_cfg.get("script")
    if script and os.path.isdir(script):
        base = script
    else:
        app_id = app_cfg.get("id", "").lower()
        base = None
        try:
            for name in os.listdir(GITHUB_BASE):
                if name.lower() == app_id and os.path.isdir(os.path.join(GITHUB_BASE, name)):
                    base = os.path.join(GITHUB_BASE, name)
                    break
        except OSError:
            pass
    if not base:
        return None
    for fname in ("docker-compose.yml", "docker-compose.yaml"):
        if os.path.isfile(os.path.join(base, fname)):
            return base
    return None


def _docker_compose(docker_dir: str, *args, timeout: int = 120) -> tuple[int, str]:
    """Run docker compose <args> in docker_dir; return (returncode, combined output)."""
    try:
        r = subprocess.run(
            ["docker", "compose", *args],
            cwd=docker_dir, capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, (r.stderr or r.stdout).strip()
    except FileNotFoundError:
        return 1, "docker not found — is Docker installed and running?"
    except subprocess.TimeoutExpired:
        return 1, f"docker compose {' '.join(args)} timed out"


def start_subprocess(app_cfg: dict) -> subprocess.Popen:
    launch_type = app_cfg.get("launch_type", "python")
    app_id = app_cfg.get("id", "unknown")
    script = app_cfg["script"]
    venv = app_cfg.get("venv")

    if os.path.isabs(script):
        script_path = script
    else:
        script_path = os.path.abspath(os.path.join(BASE_DIR, script))

    if venv:
        python_exe = os.path.join(venv, "bin", "python")
        if not _is_valid_python_binary(python_exe):
            # Venv python is missing or a text file (OneDrive corrupts symlinks).
            # Fall back to machine-local venv cache outside OneDrive.
            cached = os.path.join(VENV_CACHE_DIR, app_id, "bin", "python")
            if _is_valid_python_binary(cached):
                python_exe = cached
            else:
                raise RuntimeError(
                    f"venv Python at '{python_exe}' is not executable "
                    f"(OneDrive likely corrupted the symlink). "
                    f"Run: fd setup {app_id}"
                )
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

    log_dir = "/tmp"
    stdout_log = open(os.path.join(log_dir, f"flightdeck-{app_id}.log"), "a")
    stderr_log = open(os.path.join(log_dir, f"flightdeck-{app_id}.error.log"), "a")

    if launch_type == "uvicorn":
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


# ---------------------------------------------------------------------------
# Directory analyzer for "Add App"
# ---------------------------------------------------------------------------

def analyze_directory(path: str) -> dict:
    """
    Scan a project directory and return a best-guess FlightDeck app config.
    Returns a dict with keys: id, name, launch_type, port, script, venv,
    uvicorn_app, launch_args, health_endpoint, web_url, confidence, notes.
    """
    path = os.path.expanduser(path)
    if not os.path.isdir(path):
        return {"error": f"Directory not found: {path}"}

    result = {
        "id": "",
        "name": "",
        "launch_type": "python",
        "port": None,
        "script": None,
        "venv": None,
        "uvicorn_app": None,
        "launch_args": [],
        "health_endpoint": None,
        "web_url": None,
        "notes": [],
    }

    dirname = os.path.basename(path.rstrip("/"))
    result["name"] = dirname
    result["id"] = re.sub(r"[^a-z0-9_-]", "-", dirname.lower()).strip("-")

    notes = result["notes"]

    # --- venv ---
    for venv_name in (".venv", "venv", "env"):
        venv_path = os.path.join(path, venv_name)
        if os.path.isdir(venv_path):
            result["venv"] = venv_path
            notes.append(f"Found venv at {venv_name}/")
            break

    # --- Docker Compose ---
    for dc_name in ("docker-compose.yml", "docker-compose.yaml"):
        dc_path = os.path.join(path, dc_name)
        if os.path.isfile(dc_path):
            result["launch_type"] = "docker"
            notes.append(f"Found {dc_name} — setting launch_type=docker")
            # Parse ports from docker-compose
            try:
                with open(dc_path) as f:
                    content = f.read()
                # Look for frontend/web service ports first, then any port
                # Pattern: "HOST:CONTAINER" or just "PORT"
                port_matches = re.findall(r'["\'"]?(\d{4,5}):(\d{2,5})["\']?', content)
                # Prefer higher-numbered host ports (frontend) or 80/443 patterns
                if port_matches:
                    # Try to find a frontend/nginx/web service port
                    frontend_port = None
                    lines = content.split("\n")
                    in_frontend = False
                    for line in lines:
                        if re.search(r'(frontend|web|nginx|ui):', line, re.I):
                            in_frontend = True
                        if in_frontend and ":" in line:
                            m = re.search(r'["\'"]?(\d{4,5}):(\d{2,5})["\']?', line)
                            if m:
                                frontend_port = int(m.group(1))
                                break
                        if in_frontend and re.match(r'\S', line) and "frontend" not in line.lower():
                            in_frontend = False
                    if frontend_port:
                        result["port"] = frontend_port
                        notes.append(f"Found frontend port {frontend_port} in docker-compose")
                    else:
                        result["port"] = int(port_matches[0][0])
                        notes.append(f"Found port {result['port']} in docker-compose")
            except Exception as e:
                notes.append(f"Could not parse docker-compose ports: {e}")
            break

    if result["launch_type"] != "docker":
        # --- Requirements / pyproject: check for uvicorn/fastapi ---
        is_asgi = False
        for req_file in ("requirements.txt", "pyproject.toml", "setup.cfg"):
            req_path = os.path.join(path, req_file)
            if os.path.isfile(req_path):
                try:
                    content = open(req_path).read().lower()
                    if any(k in content for k in ("uvicorn", "fastapi", "starlette")):
                        is_asgi = True
                        notes.append(f"Found ASGI framework in {req_file}")
                        break
                except Exception:
                    pass

        # Also check backend/ subdirectory
        backend_dir = os.path.join(path, "backend")
        if not is_asgi and os.path.isdir(backend_dir):
            for req_file in ("requirements.txt", "pyproject.toml"):
                req_path = os.path.join(backend_dir, req_file)
                if os.path.isfile(req_path):
                    try:
                        content = open(req_path).read().lower()
                        if any(k in content for k in ("uvicorn", "fastapi", "starlette")):
                            is_asgi = True
                            notes.append(f"Found ASGI framework in backend/{req_file}")
                            break
                    except Exception:
                        pass

        # --- Find entry point ---
        # Priority: backend/main.py, backend/app.py, main.py, app.py, run.py, server.py
        entry_candidates = []
        for subdir in ("backend", "src", ""):
            base = os.path.join(path, subdir) if subdir else path
            for fname in ("main.py", "app.py", "run.py", "server.py", "web_app.py"):
                fp = os.path.join(base, fname)
                if os.path.isfile(fp):
                    entry_candidates.append(fp)

        if is_asgi:
            result["launch_type"] = "uvicorn"
            # Find working directory (directory containing entry file)
            if entry_candidates:
                entry = entry_candidates[0]
                result["script"] = os.path.dirname(entry)
                # Guess module name
                module_file = os.path.splitext(os.path.basename(entry))[0]
                result["uvicorn_app"] = f"{module_file}:app"
                notes.append(f"Uvicorn app: {result['uvicorn_app']} in {result['script']}")
        else:
            if entry_candidates:
                result["script"] = entry_candidates[0]
                notes.append(f"Entry point: {result['script']}")

        # --- Detect port from source files ---
        if result["port"] is None:
            port_sources = entry_candidates[:3]
            for src in port_sources:
                try:
                    content = open(src).read()
                    # PORT = 1234 or port=1234 or --port 1234
                    m = re.search(r'PORT\s*=\s*(\d{4,5})', content)
                    if not m:
                        m = re.search(r'port\s*=\s*(\d{4,5})', content, re.I)
                    if not m:
                        m = re.search(r'--port[=\s]+(\d{4,5})', content)
                    if m:
                        result["port"] = int(m.group(1))
                        notes.append(f"Found port {result['port']} in {os.path.basename(src)}")
                        break
                except Exception:
                    pass

        # --- Health endpoint ---
        all_py = entry_candidates[:3]
        for src in all_py:
            try:
                content = open(src).read()
                m = re.search(r'["\'](/(?:api/)?health(?:/\w+)?)["\']', content)
                if m:
                    result["health_endpoint"] = m.group(1)
                    notes.append(f"Found health endpoint: {result['health_endpoint']}")
                    break
            except Exception:
                pass

    # --- Static HTML app (no Python files found but has index.html) ---
    if result["launch_type"] == "python" and result["script"] is None:
        if os.path.isfile(os.path.join(path, "index.html")):
            result["launch_type"] = "static"
            result["script"] = path
            notes.append("No Python entry found; detected static HTML app")
        elif os.path.isfile(os.path.join(path, "dist", "index.html")):
            result["launch_type"] = "static"
            result["script"] = os.path.join(path, "dist")
            notes.append("Found built static app in dist/")

    # --- Set web_url ---
    if result["port"]:
        result["web_url"] = f"http://localhost:{result['port']}"

    # Check for existing port conflicts
    used_ports = [a.get("port") for a in APPS]
    if result["port"] and result["port"] in used_ports:
        conflict = next(a["id"] for a in APPS if a.get("port") == result["port"])
        notes.append(f"WARNING: port {result['port']} already used by '{conflict}'")

    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/apps", methods=["GET"])
def list_apps():
    return jsonify(format_status())


@app.route("/api/start/<app_id>", methods=["POST"])
def start_app(app_id):
    app_cfg = get_app(app_id)
    if not app_cfg:
        return jsonify({"error": "App not found"}), 404

    if app_cfg.get("launch_type") == "docker":
        if is_port_in_use(app_cfg.get("port", 0)):
            return jsonify({"message": "Already running", "status": format_status()}), 200
        docker_dir = get_docker_compose_dir(app_cfg)
        if not docker_dir:
            return jsonify({"error": f"docker-compose.yml not found for '{app_id}'"}), 500
        rc, out = _docker_compose(docker_dir, "up", "-d", "--remove-orphans")
        if rc != 0:
            return jsonify({"error": out, "status": format_status()}), 500
        statuses = format_status()
        socketio.emit("status_update", {"apps": statuses})
        return jsonify({"message": "Started", "status": statuses}), 200

    existing = running_processes.get(app_id)
    if existing and existing.poll() is None:
        return jsonify({"message": "Already running", "status": format_status()}), 200

    dep_err = _start_docker_deps(app_cfg)
    if dep_err:
        return jsonify({"error": f"Failed to start Docker dependencies: {dep_err}"}), 500

    try:
        proc = start_subprocess(app_cfg)
    except Exception as e:
        return jsonify({"error": str(e), "status": format_status()}), 500
    running_processes[app_id] = proc
    statuses = format_status()
    socketio.emit("status_update", {"apps": statuses})
    return jsonify({"message": "Started", "status": statuses}), 200


@app.route("/api/stop/<app_id>", methods=["POST"])
def stop_app(app_id):
    app_cfg = get_app(app_id)
    if app_cfg and app_cfg.get("launch_type") == "docker":
        docker_dir = get_docker_compose_dir(app_cfg)
        if not docker_dir:
            return jsonify({"error": f"docker-compose.yml not found for '{app_id}'"}), 500
        rc, out = _docker_compose(docker_dir, "down", timeout=60)
        if rc != 0:
            return jsonify({"error": out, "status": format_status()}), 500
        statuses = format_status()
        socketio.emit("status_update", {"apps": statuses})
        return jsonify({"message": "Stopped", "status": statuses}), 200

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


@app.route("/api/apps/reload", methods=["POST"])
def reload_apps():
    """Reload apps.json from disk without restarting."""
    global APPS
    APPS = load_apps()
    statuses = format_status()
    socketio.emit("status_update", {"apps": statuses})
    return jsonify({"message": f"Reloaded {len(APPS)} apps", "status": statuses})


@app.route("/api/apps/<app_id>/setup", methods=["POST"])
def setup_app_venv(app_id):
    """Create a machine-local venv (outside OneDrive) and install requirements."""
    app_cfg = get_app(app_id)
    if not app_cfg:
        return jsonify({"error": "App not found"}), 404
    if app_cfg.get("launch_type") == "docker":
        return jsonify({"error": "Docker builds run locally — use: fd setup <id>"}), 400

    # Skip if the app's own venv is valid (not corrupted by OneDrive).
    # Only create a cache venv when the original is broken.
    original_venv = app_cfg.get("venv")
    if original_venv:
        original_python = os.path.join(original_venv, "bin", "python")
        if _is_valid_python_binary(original_python):
            return jsonify({
                "message": f"Original venv is healthy — no cache needed ({original_venv})",
                "venv": original_venv,
            })

    venv_dir = os.path.join(VENV_CACHE_DIR, app_id)
    try:
        os.makedirs(VENV_CACHE_DIR, exist_ok=True)
        r = subprocess.run(
            [sys.executable, "-m", "venv", venv_dir, "--clear"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return jsonify({"error": f"venv creation failed: {r.stderr or r.stdout}"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "venv creation timed out"}), 500
    except Exception as e:
        return jsonify({"error": f"venv creation failed: {e}"}), 500

    req = _find_requirements(app_cfg)
    if not req:
        return jsonify({
            "message": f"venv created at {venv_dir} (no requirements.txt found — start manually)",
            "venv": venv_dir,
        })

    python_exe = os.path.join(venv_dir, "bin", "python")
    try:
        r = subprocess.run(
            [python_exe, "-m", "pip", "install", "-q", "-r", req],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            return jsonify({"error": f"pip install failed: {(r.stderr or r.stdout)[-500:]}"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "pip install timed out (>5 min)"}), 500
    except Exception as e:
        return jsonify({"error": f"pip install failed: {e}"}), 500

    return jsonify({
        "message": f"Ready — installed requirements from {os.path.relpath(req, os.path.dirname(req))}",
        "venv": venv_dir,
    })


@app.route("/api/start", methods=["POST"])
def start_all():
    started, skipped, errors = [], [], []
    for app_cfg in APPS:
        app_id = app_cfg.get("id")
        port = app_cfg.get("port")

        if app_cfg.get("launch_type") == "docker":
            if port and is_port_in_use(port):
                skipped.append(app_id)
                continue
            docker_dir = get_docker_compose_dir(app_cfg)
            if not docker_dir:
                errors.append(f"{app_id}: docker-compose.yml not found")
                continue
            rc, out = _docker_compose(docker_dir, "up", "-d", "--remove-orphans")
            if rc != 0:
                errors.append(f"{app_id}: {out}")
            else:
                started.append(app_id)
            continue

        existing = running_processes.get(app_id)
        if existing and existing.poll() is None:
            skipped.append(app_id)
            continue
        if port and is_port_in_use(port):
            skipped.append(app_id)
            continue
        try:
            proc = start_subprocess(app_cfg)
            running_processes[app_id] = proc
            started.append(app_id)
        except Exception as e:
            errors.append(f"{app_id}: {e}")
    statuses = format_status()
    socketio.emit("status_update", {"apps": statuses})
    return jsonify({"started": started, "skipped": skipped, "errors": errors, "status": statuses})


@app.route("/api/stop", methods=["POST"])
def stop_all():
    stopped, skipped, errors = [], [], []
    for app_id, proc in list(running_processes.items()):
        if proc.poll() is not None:
            running_processes.pop(app_id, None)
            continue
        try:
            stop_process_group(proc)
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception as e:
            errors.append(f"{app_id}: {e}")
            continue
        running_processes.pop(app_id, None)
        stopped.append(app_id)

    for app_cfg in APPS:
        if app_cfg.get("launch_type") != "docker":
            continue
        app_id = app_cfg.get("id")
        port = app_cfg.get("port")
        if port and not is_port_in_use(port):
            continue
        docker_dir = get_docker_compose_dir(app_cfg)
        if not docker_dir:
            continue
        rc, out = _docker_compose(docker_dir, "down", timeout=60)
        if rc != 0:
            errors.append(f"{app_id}: {out}")
        else:
            stopped.append(app_id)

    statuses = format_status()
    socketio.emit("status_update", {"apps": statuses})
    return jsonify({"stopped": stopped, "skipped": skipped, "errors": errors, "status": statuses})


def validate_app_dir(path: str) -> tuple[bool, str]:
    """Return (valid, reason) — a valid app dir has README.md + a .py/.sh/index.html."""
    try:
        files = os.listdir(path)
    except PermissionError:
        return False, "Permission denied"
    if "README.md" not in files:
        return False, "Missing README.md"
    has_code = any(
        f.endswith(".py") or f.endswith(".sh") or f == "index.html"
        for f in files
    )
    if not has_code:
        return False, "No .py, .sh, or index.html found"
    return True, ""


@app.route("/api/browse", methods=["GET"])
def browse_directory():
    path = request.args.get("path", "").strip()
    if not path:
        path = GITHUB_BASE
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return jsonify({"error": f"Not a directory: {path}"}), 400

    parent = os.path.dirname(path)
    if parent == path:
        parent = None  # filesystem root

    try:
        entries = []
        for name in sorted(os.listdir(path)):
            if name.startswith("."):
                continue
            full = os.path.join(path, name)
            if not os.path.isdir(full):
                continue
            valid, reason = validate_app_dir(full)
            entries.append({"name": name, "path": full, "valid": valid, "reason": reason})
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403

    current_valid, current_reason = validate_app_dir(path)
    return jsonify({
        "path": path,
        "parent": parent,
        "valid": current_valid,
        "reason": current_reason,
        "dirs": entries,
    })


@app.route("/api/apps/analyze", methods=["POST"])
def analyze_app():
    """Analyze a directory and return a suggested app config."""
    data = request.get_json(silent=True) or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    result = analyze_directory(path)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/apps/add", methods=["POST"])
def add_app():
    """Add a new app to apps.json."""
    data = request.get_json(silent=True) or {}

    required = ["id", "name", "port", "launch_type"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"'{field}' is required"}), 400

    app_id = data["id"]
    # Check for duplicate
    if get_app(app_id):
        return jsonify({"error": f"App '{app_id}' already exists"}), 409

    port = int(data["port"])
    force = bool(data.get("force", False))

    # Check port conflict in apps.json
    for existing in APPS:
        if existing.get("port") == port:
            return jsonify({"error": f"Port {port} already used by '{existing['id']}'"}), 409

    # Check live port usage (external process)
    if not force and is_port_in_use(port):
        return jsonify({
            "error": f"Port {port} is currently in use by an external process",
            "port_in_use": True,
        }), 409

    launch_type = data["launch_type"]

    new_app = {
        "id": app_id,
        "name": data["name"],
        "script": data.get("script") or None,
        "venv": data.get("venv") or None,
        "url": f"http://localhost:{port}",
        "web_url": data.get("web_url") or f"http://localhost:{port}",
        "health_endpoint": data.get("health_endpoint") or None,
        "launch_type": launch_type,
        "port": port,
    }

    if launch_type == "uvicorn":
        new_app["uvicorn_app"] = data.get("uvicorn_app", "app:app")
    if launch_type == "python" and data.get("launch_args"):
        new_app["launch_args"] = data["launch_args"]

    # Handle static → generate server.py
    if launch_type == "static":
        script_dir = data.get("script", "").strip()
        if not script_dir or not os.path.isdir(script_dir):
            return jsonify({"error": f"script directory not found: {script_dir}"}), 400
        server_path = os.path.join(script_dir, "server.py")
        if not os.path.exists(server_path):
            server_src = f'''#!/usr/bin/env python3
"""{ data["name"] } static file server — managed by FlightDeck."""
import http.server, socketserver, os
PORT = {port}
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)
    def log_message(self, format, *args): pass
if __name__ == "__main__":
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"{ data["name"] } running at http://0.0.0.0:{{PORT}}")
        try: httpd.serve_forever()
        except KeyboardInterrupt: pass
'''
            with open(server_path, "w") as f:
                f.write(server_src)
            os.chmod(server_path, 0o755)
        new_app["script"] = server_path
        new_app["launch_type"] = "python"

    updated = APPS + [new_app]
    save_apps(updated)

    statuses = format_status()
    socketio.emit("status_update", {"apps": statuses})
    return jsonify({"message": f"App '{app_id}' added", "app": new_app, "status": statuses}), 201


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
    app_cfg = get_app(app_id)
    if not app_cfg:
        return jsonify({"error": "App not found"}), 404
    health = check_app_health(app_cfg)
    return jsonify(health)


@app.route("/api/health", methods=["GET"])
def get_all_health():
    results = {}
    for app_cfg in APPS:
        app_id = app_cfg.get("id")
        proc = running_processes.get(app_id)
        if proc and proc.poll() is None:
            results[app_id] = check_app_health(app_cfg)
        else:
            results[app_id] = {"healthy": False, "error": "Not running"}
    return jsonify(results)


@app.route("/api/apps/<app_id>/autostart", methods=["POST"])
def toggle_autostart(app_id):
    """Toggle the autostart flag for an app and persist it to apps.json."""
    app_cfg = get_app(app_id)
    if not app_cfg:
        return jsonify({"error": "App not found"}), 404

    if app_cfg.get("launch_type") == "docker":
        return jsonify({"error": "Docker-managed apps cannot use autostart"}), 400

    data = request.get_json(silent=True) or {}
    # Accept explicit value or just flip current
    if "autostart" in data:
        new_val = bool(data["autostart"])
    else:
        new_val = not app_cfg.get("autostart", False)

    app_cfg["autostart"] = new_val
    save_apps(APPS)

    statuses = format_status()
    socketio.emit("status_update", {"apps": statuses})
    return jsonify({"autostart": new_val, "status": statuses})


def launch_autostart_apps() -> None:
    """Start all apps flagged autostart=true that aren't already running."""
    for app_cfg in APPS:
        if not app_cfg.get("autostart", False):
            continue
        if app_cfg.get("launch_type") == "docker":
            continue
        app_id = app_cfg.get("id")
        port = app_cfg.get("port")
        # Skip if already listening (e.g. re-started FlightDeck without reboot)
        if port and is_port_in_use(port):
            print(f"[autostart] {app_id} already running on port {port}, skipping")
            continue
        try:
            dep_err = _start_docker_deps(app_cfg)
            if dep_err:
                print(f"[autostart] docker deps failed for {app_id}: {dep_err}", file=sys.stderr)
                continue
            print(f"[autostart] starting {app_id}…")
            proc = start_subprocess(app_cfg)
            running_processes[app_id] = proc
        except Exception as e:
            print(f"[autostart] failed to start {app_id}: {e}", file=sys.stderr)


def background_health_checker():
    while True:
        time.sleep(10)
        for app_cfg in APPS:
            app_id = app_cfg.get("id")
            port = app_cfg.get("port")
            proc = running_processes.get(app_id)
            is_managed = proc and proc.poll() is None
            if is_managed or (port and is_port_in_use(port)):
                check_app_health(app_cfg)
        try:
            socketio.emit("status_update", {"apps": format_status()})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Repo management helpers
# ---------------------------------------------------------------------------

GITHUB_BASE = os.path.expanduser(
    "~/Library/CloudStorage/OneDrive-Personal/Projects/GitHub"
)

REQUIRED_GITIGNORE = [
    ".env", "*.pem", "*.key", ".venv", "__pycache__",
    ".DS_Store", "*.log", "secrets.*", "credentials.*",
]

SECRET_FILE_PATTERNS = [
    re.compile(r"(^|/)\.env$"),
    re.compile(r"\.pem$"),
    re.compile(r"\.key$"),
    re.compile(r"(^|/)id_rsa$"),
    re.compile(r"(^|/)id_ed25519$"),
    re.compile(r"secrets\.(json|ya?ml)$"),
    re.compile(r"credentials\.(json|ya?ml)$"),
]
SECRET_ALLOWLIST = re.compile(r"\.(example|sample|template|test)$")

CREDENTIAL_REGEXES = [
    ("AWS access key",           re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Private key block",        re.compile(r"-----BEGIN.{0,20}PRIVATE KEY-----")),
    ("GitHub PAT",               re.compile(r"ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{82}")),
    ("Hardcoded password",       re.compile(r'password\s*=\s*["\'][^"\']{6,}["\']', re.I)),
    ("Hardcoded secret",         re.compile(r'secret\s*=\s*["\'][^"\']{8,}["\']', re.I)),
    ("Hardcoded API key",        re.compile(r'api_key\s*=\s*["\'][^"\']{8,}["\']', re.I)),
    ("Hardcoded access token",   re.compile(r'access_token\s*=\s*["\'][^"\']{8,}["\']', re.I)),
    ("Stripe live key",          re.compile(r"sk_live_[0-9a-zA-Z]{24}")),
]
SKIP_EXTENSIONS = {
    "jpg","jpeg","png","gif","svg","ico","woff","woff2","ttf","eot",
    "mp3","m4b","epub","pdf","zip","tar","gz","db","sqlite","sqlite3",
    "pyc","so","o","a","class","jar","lock","png","webp",
}
ARTEFACT_DIRS = {".venv","venv","node_modules","__pycache__",".pytest_cache",".mypy_cache"}


def get_repo_root(app_cfg: dict) -> Optional[str]:
    """Derive the git repo root directory for an app."""
    script = app_cfg.get("script")
    app_id = app_cfg.get("id", "")

    if not script:
        # Docker or no-script app: scan GITHUB_BASE for matching dir name
        id_lower = app_id.lower()
        try:
            for name in os.listdir(GITHUB_BASE):
                if name.lower() == id_lower:
                    candidate = os.path.join(GITHUB_BASE, name)
                    if os.path.isdir(candidate):
                        return candidate
        except OSError:
            pass
        return None

    base = script if os.path.isdir(script) else os.path.dirname(script)
    # Walk up to find .git
    current = os.path.abspath(base)
    while current != os.path.dirname(current):
        if os.path.isdir(os.path.join(current, ".git")):
            return current
        current = os.path.dirname(current)
    return None


def _git(repo: str, *args, timeout: int = 15) -> tuple[int, str, str]:
    """Run a git command in repo; returns (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, timeout=timeout
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def repo_status(repo: str) -> dict:
    """Return a rich status dict for a git repo."""
    _, branch, _ = _git(repo, "branch", "--show-current")
    _, remote_url, _ = _git(repo, "remote", "get-url", "origin")
    _, log1, _ = _git(repo, "log", "-1", "--pretty=%h %s (%ar)", "--no-walk")

    # Fetch quietly so ahead/behind is up to date
    _git(repo, "fetch", "origin", "--quiet", timeout=20)

    behind = ahead = 0
    if branch and remote_url:
        _, b, _ = _git(repo, "rev-list", "--count", f"HEAD..origin/{branch}")
        _, a, _ = _git(repo, "rev-list", "--count", f"origin/{branch}..HEAD")
        behind = int(b) if b.isdigit() else 0
        ahead  = int(a) if a.isdigit() else 0

    _, status_out, _ = _git(repo, "status", "--porcelain")
    dirty_files = [l for l in status_out.splitlines() if l.strip()]

    return {
        "branch": branch or "unknown",
        "remote": remote_url or None,
        "last_commit": log1 or None,
        "ahead": ahead,
        "behind": behind,
        "dirty": len(dirty_files) > 0,
        "dirty_count": len(dirty_files),
        "dirty_files": dirty_files[:20],
    }


def run_hygiene(repo: str) -> dict:
    """Run hygiene checks on a repo and return a structured report."""
    checks = []

    def check(name, passed, detail=None, fix_available=False):
        checks.append({
            "name": name,
            "passed": passed,
            "detail": detail,
            "fix_available": fix_available,
        })

    # .gitignore completeness
    gi_path = os.path.join(repo, ".gitignore")
    missing = []
    if os.path.isfile(gi_path):
        gi_content = open(gi_path).read()
        missing = [p for p in REQUIRED_GITIGNORE if p not in gi_content]
    else:
        missing = REQUIRED_GITIGNORE[:]

    if missing:
        check(".gitignore coverage", False,
              f"Missing: {', '.join(missing)}", fix_available=True)
    else:
        check(".gitignore coverage", True)

    # Secret files tracked
    _, ls_out, _ = _git(repo, "ls-files")
    tracked = ls_out.splitlines()
    secret_hits = []
    for f in tracked:
        bn = os.path.basename(f)
        if SECRET_ALLOWLIST.search(bn):
            continue
        if any(pat.search(f) for pat in SECRET_FILE_PATTERNS):
            secret_hits.append(f)

    if secret_hits:
        check("No secret files tracked", False,
              f"Tracked: {', '.join(secret_hits[:5])}")
    else:
        check("No secret files tracked", True)

    # Credential pattern scan in source files
    cred_hits = []
    text_files = [f for f in tracked
                  if f.rsplit(".", 1)[-1].lower() not in SKIP_EXTENSIONS
                  and not f.endswith("/")]
    for f in text_files[:200]:   # cap at 200 files for speed
        full = os.path.join(repo, f)
        try:
            content = open(full, errors="ignore").read(50_000)
        except OSError:
            continue
        for label, pat in CREDENTIAL_REGEXES:
            if pat.search(content):
                cred_hits.append(f"{label}: {f}")
                break

    if cred_hits:
        check("No credentials in source files", False,
              "; ".join(cred_hits[:5]))
    else:
        check("No credentials in source files", True)

    # README exists
    readme = os.path.join(repo, "README.md")
    if os.path.isfile(readme):
        lines = open(readme).read().count("\n")
        if lines < 6:
            check("README.md", False, f"Only {lines} lines — very sparse")
        else:
            check("README.md", True, f"{lines} lines")
    else:
        check("README.md", False, "File missing")

    # Build artefacts tracked
    artefact_hits = []
    for d in ARTEFACT_DIRS:
        count = sum(1 for f in tracked if f.startswith(d + "/"))
        if count:
            artefact_hits.append(f"{d}/ ({count} files)")
    if artefact_hits:
        check("No build artefacts tracked", False,
              ", ".join(artefact_hits))
    else:
        check("No build artefacts tracked", True)

    # Large files
    large = []
    for f in tracked[:500]:
        full = os.path.join(repo, f)
        try:
            sz = os.path.getsize(full)
            if sz > 500_000:
                large.append(f"{f} ({sz // 1024} KB)")
        except OSError:
            pass
    if large:
        check("No large files (>500 KB)", False, ", ".join(large[:5]))
    else:
        check("No large files (>500 KB)", True)

    passed = sum(1 for c in checks if c["passed"])
    return {
        "checks": checks,
        "passed": passed,
        "total": len(checks),
        "fix_available": any(c["fix_available"] for c in checks),
        "missing_gitignore": missing,
    }


def apply_hygiene_fix(repo: str, missing_patterns: list) -> dict:
    """Append missing patterns to .gitignore and commit."""
    gi_path = os.path.join(repo, ".gitignore")
    with open(gi_path, "a") as f:
        f.write("\n# Security / hygiene (added by FlightDeck)\n")
        for pat in missing_patterns:
            f.write(pat + "\n")

    _git(repo, "add", ".gitignore")
    rc, out, err = _git(repo, "commit", "-m",
                        "chore: hygiene — add missing .gitignore entries\n\nApplied by FlightDeck")
    if rc != 0:
        return {"error": f"Commit failed: {err}"}
    return {"committed": True, "patterns": missing_patterns}


# ---------------------------------------------------------------------------
# Repo API routes
# ---------------------------------------------------------------------------

@app.route("/api/repo/<app_id>", methods=["GET"])
def get_repo_status(app_id):
    app_cfg = get_app(app_id)
    if not app_cfg:
        return jsonify({"error": "App not found"}), 404
    repo = get_repo_root(app_cfg)
    if not repo:
        return jsonify({"error": "Repo directory not found"}), 404
    if not os.path.isdir(os.path.join(repo, ".git")):
        return jsonify({"error": "Not a git repository"}), 400
    try:
        status = repo_status(repo)
        status["repo_path"] = repo
        return jsonify(status)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Git operation timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/repo/<app_id>/pull", methods=["POST"])
def pull_repo(app_id):
    app_cfg = get_app(app_id)
    if not app_cfg:
        return jsonify({"error": "App not found"}), 404
    repo = get_repo_root(app_cfg)
    if not repo:
        return jsonify({"error": "Repo directory not found"}), 404
    rc, out, err = _git(repo, "pull", "--ff-only", timeout=30)
    if rc != 0:
        return jsonify({"error": err or out}), 400
    return jsonify({"message": out or "Already up to date."})


@app.route("/api/repo/pull-all", methods=["POST"])
def pull_all_repos():
    results = []
    seen_repos = {}  # repo_path -> app_id (dedup)
    for app_cfg in APPS:
        app_id = app_cfg["id"]
        repo = get_repo_root(app_cfg)
        if not repo or not os.path.isdir(os.path.join(repo, ".git")):
            results.append({"app_id": app_id, "skipped": True, "reason": "no git repo"})
            continue
        if repo in seen_repos:
            results.append({"app_id": app_id, "skipped": True, "reason": f"same repo as {seen_repos[repo]}"})
            continue
        seen_repos[repo] = app_id
        try:
            rc, out, err = _git(repo, "pull", "--ff-only", timeout=30)
            if rc != 0:
                results.append({"app_id": app_id, "repo": repo, "error": err or out})
            else:
                results.append({"app_id": app_id, "repo": repo, "message": out or "Already up to date."})
        except subprocess.TimeoutExpired:
            results.append({"app_id": app_id, "repo": repo, "error": "Timed out"})

    pulled  = [r["app_id"] for r in results if "message" in r]
    skipped = [r["app_id"] for r in results if r.get("skipped")]
    errors  = [f"{r['app_id']}: {r['error']}" for r in results if "error" in r and not r.get("skipped")]
    return jsonify({"results": results, "pulled": pulled, "skipped": skipped, "errors": errors})


@app.route("/api/repo/push-all", methods=["POST"])
def push_all_repos():
    results = []
    seen_repos = {}
    for app_cfg in APPS:
        app_id = app_cfg["id"]
        repo = get_repo_root(app_cfg)
        if not repo or not os.path.isdir(os.path.join(repo, ".git")):
            results.append({"app_id": app_id, "skipped": True, "reason": "no git repo"})
            continue
        if repo in seen_repos:
            results.append({"app_id": app_id, "skipped": True, "reason": f"same repo as {seen_repos[repo]}"})
            continue
        seen_repos[repo] = app_id
        try:
            rc, out, err = _git(repo, "push", "origin", "HEAD", timeout=30)
            if rc != 0:
                results.append({"app_id": app_id, "repo": repo, "error": err or out})
            else:
                results.append({"app_id": app_id, "repo": repo, "message": out or "Pushed."})
        except subprocess.TimeoutExpired:
            results.append({"app_id": app_id, "repo": repo, "error": "Timed out"})

    pushed  = [r["app_id"] for r in results if "message" in r]
    skipped = [r["app_id"] for r in results if r.get("skipped")]
    errors  = [f"{r['app_id']}: {r['error']}" for r in results if "error" in r and not r.get("skipped")]
    return jsonify({"results": results, "pushed": pushed, "skipped": skipped, "errors": errors})


@app.route("/api/repo/commit-all", methods=["POST"])
def commit_all_repos():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "commit message is required"}), 400

    results = []
    seen_repos = {}
    for app_cfg in APPS:
        app_id = app_cfg["id"]
        repo = get_repo_root(app_cfg)
        if not repo or not os.path.isdir(os.path.join(repo, ".git")):
            results.append({"app_id": app_id, "skipped": True, "reason": "no git repo"})
            continue
        if repo in seen_repos:
            results.append({"app_id": app_id, "skipped": True, "reason": f"same repo as {seen_repos[repo]}"})
            continue
        seen_repos[repo] = app_id
        _git(repo, "add", "-A")
        rc, out, err = _git(repo, "commit", "-m", message)
        if rc != 0:
            # "nothing to commit" is not a real error
            if "nothing to commit" in (out + err):
                results.append({"app_id": app_id, "repo": repo, "skipped": True, "reason": "nothing to commit"})
            else:
                results.append({"app_id": app_id, "repo": repo, "error": err or out})
        else:
            results.append({"app_id": app_id, "repo": repo, "message": out.splitlines()[0] if out else "Committed."})

    committed = [r["app_id"] for r in results if "message" in r]
    skipped   = [r["app_id"] for r in results if r.get("skipped")]
    errors    = [f"{r['app_id']}: {r['error']}" for r in results if "error" in r and not r.get("skipped")]
    return jsonify({"results": results, "committed": committed, "skipped": skipped, "errors": errors})


@app.route("/api/repo/<app_id>/push", methods=["POST"])
def push_repo(app_id):
    app_cfg = get_app(app_id)
    if not app_cfg:
        return jsonify({"error": "App not found"}), 404
    repo = get_repo_root(app_cfg)
    if not repo:
        return jsonify({"error": "Repo directory not found"}), 404
    rc, out, err = _git(repo, "push", "origin", "HEAD", timeout=30)
    if rc != 0:
        return jsonify({"error": err or out}), 400
    return jsonify({"message": out or "Pushed."})


@app.route("/api/repo/<app_id>/commit", methods=["POST"])
def commit_repo(app_id):
    app_cfg = get_app(app_id)
    if not app_cfg:
        return jsonify({"error": "App not found"}), 404
    repo = get_repo_root(app_cfg)
    if not repo:
        return jsonify({"error": "Repo directory not found"}), 404
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "commit message is required"}), 400
    files = data.get("files") or ["-A"]   # list of paths or ["-A"] for all
    _git(repo, "add", *files)
    rc, out, err = _git(repo, "commit", "-m", message)
    if rc != 0:
        return jsonify({"error": err or out}), 400
    return jsonify({"message": out})


@app.route("/api/repo/<app_id>/hygiene", methods=["GET"])
def get_hygiene(app_id):
    app_cfg = get_app(app_id)
    if not app_cfg:
        return jsonify({"error": "App not found"}), 404
    repo = get_repo_root(app_cfg)
    if not repo:
        return jsonify({"error": "Repo directory not found"}), 404
    if not os.path.isdir(os.path.join(repo, ".git")):
        return jsonify({"error": "Not a git repository"}), 400
    try:
        result = run_hygiene(repo)
        result["repo_path"] = repo
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/repo/<app_id>/hygiene/fix", methods=["POST"])
def fix_hygiene(app_id):
    app_cfg = get_app(app_id)
    if not app_cfg:
        return jsonify({"error": "App not found"}), 404
    repo = get_repo_root(app_cfg)
    if not repo:
        return jsonify({"error": "Repo directory not found"}), 404
    report = run_hygiene(repo)
    missing = report.get("missing_gitignore", [])
    if not missing:
        return jsonify({"message": "Nothing to fix"})
    result = apply_hygiene_fix(repo, missing)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


# Launch autostart apps in a background thread so Docker deps don't block Flask startup
autostart_thread = threading.Thread(target=launch_autostart_apps, daemon=True)
autostart_thread.start()

health_thread = threading.Thread(target=background_health_checker, daemon=True)
health_thread.start()


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5050)
