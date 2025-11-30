# FlightDeck

A cockpit-style local development dashboard for managing your development apps.

## Quick Start

### Run FlightDeck manually:
```bash
./start.sh
```

This will:
- Start the backend API on port 5000
- Start the homepage server on port 3325
- Open FlightDeck in your browser

Press `Ctrl+C` to stop all services.

## Auto-Start on Mac Login

### Enable auto-start:
```bash
cp config/com.flightdeck.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.flightdeck.plist
```

### Disable auto-start:
```bash
launchctl unload ~/Library/LaunchAgents/com.flightdeck.plist
rm ~/Library/LaunchAgents/com.flightdeck.plist
```

## Features

- **Cockpit-style UI** with toggles, switches, knobs, and gauges
- **Start/Stop buttons** for each app
- **Live status indicators**:
  - 🟢 Green = App is running
  - 🟡 Amber = App is starting
  - 🔴 Red = App is offline
- **Real-time clock and uptime tracker**
- **Auto-refreshing status** (polls every 5 seconds)

## Configuration

Edit `backend/apps.json` to add or modify apps:

```json
{
  "id": "myapp",
  "name": "My App",
  "script": "../path/to/start_script.py",
  "url": "http://localhost:PORT"
}
```

## URLs

- **FlightDeck Homepage**: http://localhost:3325
- **Backend API**: http://localhost:5000

## Requirements

- Python 3
- Flask, Flask-CORS, Flask-SocketIO, eventlet

Install dependencies:
```bash
cd backend
pip install flask flask-cors flask-socketio eventlet
```

---

## Project Layout
```
FlightDeck/
├── backend/          # Flask + Socket.IO API and process manager
│   ├── app.py        # Backend server
│   └── apps.json     # App configuration
├── homepage/         # Cockpit-style static homepage
│   ├── index.html    # Main UI
│   ├── server.py     # Simple HTTP server
│   └── images/       # App icons
├── start.sh          # Single startup script
└── com.flightdeck.plist  # LaunchAgent for auto-start
```

## API Endpoints

- `GET /api/apps` - List all apps and their statuses
- `POST /api/start/<app_id>` - Start an app
- `POST /api/stop/<app_id>` - Stop an app
- WebSocket: Subscribe to `status_update` events via Socket.IO
