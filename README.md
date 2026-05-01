# FlightDeck

A cockpit-style local dashboard for managing, monitoring, and maintaining your development apps — with a full CLI, real-time web UI, and git repo management built in.

---

## Quick Start

```bash
./start.sh          # start FlightDeck manually (opens browser)
```

- Homepage: **http://localhost:3325**
- Backend API: **http://localhost:5050**

---

## CLI — `fd`

The `fd` CLI talks to the FlightDeck backend API. It is symlinked to `/usr/local/bin/fd` so it works from any directory.

### App lifecycle

```bash
fd status                      # table of all apps — running, healthy, URL
fd start <id>                  # start one app
fd start --all                 # start all manageable apps
fd stop  <id>                  # stop one app
fd stop  --all                 # stop all running apps
fd restart <id>                # stop then start
fd open   <id>                 # open app URL in browser
fd reload                      # reload apps.json from disk (no restart needed)
```

### Git & repo management

```bash
fd repo   <id>                 # show branch, ahead/behind, dirty files
fd pull   <id>                 # git pull one repo
fd pull   --all                # git pull all repos (deduplicates shared repos)
fd commit --all                # interactive — prompts for a unique message per repo
fd commit -m "msg" <id>        # git add -A + commit one repo
fd commit -m "msg" --all       # same message to all repos
fd push   <id>                 # git push one repo
fd push   --all                # git push all repos
```

### Hygiene

```bash
fd hygiene <id>                # run 7-point hygiene check on one repo
fd hygiene <id> fix            # auto-fix .gitignore gaps
```

Hygiene checks: `.gitignore` coverage · no secret files tracked · no credentials in source · README.md present · no build artefacts tracked · no large files.

### App management

```bash
fd add <path>                  # analyze a directory and register it as a new app
fd add -y <path>               # same, without confirmation prompt (scriptable)
fd autostart <id>              # toggle autostart on/off for one app
```

`fd add` shows the current app registry, runs the directory analyzer, displays the suggested config, then prompts for confirmation. If the port is already in use by an external process, it warns and offers to force-add.

### Configuration

```bash
fd config                              # show current settings
fd config start-at-login on            # install login launch agent (macOS)
fd config start-at-login off           # remove it
```

---

## Web UI

Open **http://localhost:3325** in your browser.

### Fleet toolbar
| Button | Action |
|---|---|
| ▶ Start All | Start all manageable apps |
| ■ Stop All | Stop all running apps |
| ⬇ Pull All | `git pull` all repos |
| ⬆ Push All | `git push` all repos |
| ✎ Commit All | Modal — enter a unique commit message per repo |
| ↺ Reload | Reload `apps.json` from disk |

### Per-app cards
- **Start / Stop** toggle button
- **Open** — launch app URL in browser
- **⚡ Autostart** — mark app to start automatically when FlightDeck starts
- **Repo button** — opens the Repo panel (see below)
- Live health indicator — green / red / dim based on health endpoint

### Repo panel (per app)
**Status tab:** branch, remote URL, last commit, ahead/behind count, dirty files list, Pull / Push / Commit buttons.

**Hygiene tab:** run the 7-point hygiene check, view per-check results, auto-fix `.gitignore` gaps.

### Add App modal
Click the **＋ Add App** card to register a new app.

- **Browse** button — inline file-system navigator. Directories are marked ✓ (valid) or — (invalid) based on whether they contain a `README.md` plus at least one `.py`, `.sh`, or `index.html` file.
- **Scan** — auto-detects launch type, port, entry point, venv, and health endpoint.
- Review the suggested config, edit if needed, then click **Add to FlightDeck**.

If the port is in use by an external process, an **Add Anyway** button appears.

---

## Start at Login (macOS)

```bash
fd config start-at-login on
```

This copies `config/com.flightdeck.plist` to `~/Library/LaunchAgents/` and loads it with `launchctl`. FlightDeck starts automatically on every login. The browser does **not** open automatically (suppressed via `FLIGHTDECK_NO_BROWSER=1`).

To disable:
```bash
fd config start-at-login off
```

### Per-app autostart

Each app can be individually marked to start when FlightDeck starts:

```bash
fd autostart <id>              # toggle via CLI
```

Or use the ⚡ button on each card in the web UI. Apps flagged `autostart: true` in `apps.json` are started by `launch_autostart_apps()` on backend startup, skipping any app whose port is already occupied.

---

## Tailscale Access

Both servers bind to `0.0.0.0`, so they are reachable over Tailscale without any extra configuration:

| | URL |
|---|---|
| Web UI | `http://100.91.79.68:3325` |
| Backend API | `http://100.91.79.68:5050` |

Replace `100.91.79.68` with your current Tailscale IP (`tailscale ip -4`).

---

## apps.json — App Configuration

Each entry in `backend/apps.json`:

```json
{
  "id": "myapp",
  "name": "My App",
  "script": "/path/to/script.py",
  "venv": "/path/to/.venv",
  "url": "http://localhost:8080",
  "web_url": "http://localhost:8080",
  "health_endpoint": "/health",
  "launch_type": "python",
  "port": 8080,
  "autostart": false
}
```

| Field | Values | Notes |
|---|---|---|
| `launch_type` | `python` `uvicorn` `static` `docker` | Docker apps are monitored only, not managed |
| `uvicorn_app` | `"main:app"` | Required for `uvicorn` type |
| `launch_args` | `["--flag", "value"]` | Extra args for `python` type |
| `health_endpoint` | `"/health"` or `null` | Polled every 10 s; `null` = no health check |
| `autostart` | `true` / `false` | Start automatically when FlightDeck starts |

Edit `apps.json` manually and run `fd reload` (or click ↺ Reload) to apply without restarting FlightDeck.

### apps.json backups

Every write to `apps.json` (add app, toggle autostart, etc.) creates a timestamped backup in `backend/backups/`. The 10 most recent backups are kept automatically.

---

## Adding a New App

### Via the CLI
```bash
fd add /path/to/your/project
```

The directory must contain a `README.md` and at least one `.py`, `.sh`, or `index.html` file.

### Via the Web UI
Click the **＋ Add App** card → Browse or type the path → Scan → review → Add to FlightDeck.

### Via directory analysis API
```bash
curl -s -X POST http://localhost:5050/api/apps/analyze \
  -H 'Content-Type: application/json' \
  -d '{"path": "/path/to/project"}' | jq .
```

---

## API Reference

### App lifecycle
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/apps` | List all apps with status, health, PID |
| `POST` | `/api/start/<id>` | Start one app |
| `POST` | `/api/stop/<id>` | Stop one app |
| `POST` | `/api/start` | Start all manageable apps |
| `POST` | `/api/stop` | Stop all running apps |
| `POST` | `/api/apps/reload` | Reload apps.json from disk |
| `GET` | `/api/health/<id>` | Health check one app |
| `GET` | `/api/health` | Health check all running apps |

### App management
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/browse?path=<dir>` | List subdirectories with validity indicators |
| `POST` | `/api/apps/analyze` | Analyze a directory, return suggested config |
| `POST` | `/api/apps/add` | Add a new app (pass `"force": true` to override live port check) |
| `POST` | `/api/apps/<id>/autostart` | Toggle autostart flag |

### Git / repo
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/repo/<id>` | Repo status: branch, ahead/behind, dirty files |
| `POST` | `/api/repo/<id>/pull` | `git pull --ff-only` |
| `POST` | `/api/repo/<id>/push` | `git push origin HEAD` |
| `POST` | `/api/repo/<id>/commit` | `git add -A` + commit (body: `{"message": "..."}`) |
| `POST` | `/api/repo/pull-all` | Pull all repos (deduplicated) |
| `POST` | `/api/repo/push-all` | Push all repos |
| `POST` | `/api/repo/commit-all` | Commit all with one message (body: `{"message": "..."}`) |
| `GET` | `/api/repo/<id>/hygiene` | Run hygiene checks |
| `POST` | `/api/repo/<id>/hygiene/fix` | Auto-fix `.gitignore` issues |

### WebSocket (Socket.IO)
Connect to `http://localhost:5050`. Subscribe to `status_update` events for real-time app state changes.

---

## Project Layout

```
FlightDeck/
├── backend/
│   ├── app.py           # Flask + Socket.IO API, process manager
│   ├── apps.json        # App registry
│   ├── backups/         # Auto-created timestamped backups of apps.json
│   └── requirements.txt
├── config/
│   └── com.flightdeck.plist   # macOS LaunchAgent (install via fd config)
├── homepage/
│   ├── index.html       # Cockpit-style web UI
│   ├── server.py        # Static file server (0.0.0.0:3325)
│   └── images/
├── scripts/
│   └── fd               # CLI (symlinked to /usr/local/bin/fd)
├── start.sh             # Manual startup script
└── docker-compose.yml
```

---

## Requirements

- Python 3.9+
- `jq` (for `fd` CLI) — `brew install jq`

Install backend dependencies:
```bash
cd backend
pip install flask flask-cors flask-socketio eventlet requests
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `FLIGHTDECK_API` | `http://localhost:5050` | API base URL used by the `fd` CLI |
| `FLIGHTDECK_NO_BROWSER` | `0` | Set to `1` to suppress browser open on `./start.sh` |
