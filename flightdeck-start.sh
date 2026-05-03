#!/bin/bash
# FlightDeck Startup Script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure common tool paths are available (Homebrew, Docker Desktop, pyenv, etc.)
export PATH="/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"

# Resolve python3
PYTHON=$(command -v python3 || command -v python)
if [[ -z "$PYTHON" ]]; then
    echo "❌ Python not found. Install Python 3 and try again."
    exit 1
fi

echo "🛩️  Starting Flight Deck..."

# Create venv if it doesn't exist
VENV="$SCRIPT_DIR/backend/.venv"
if [[ ! -d "$VENV" ]]; then
    echo "   Creating virtual environment..."
    "$PYTHON" -m venv "$VENV"
fi
PYTHON="$VENV/bin/python"

# Install/sync backend dependencies
echo "   Checking backend dependencies..."
if ! "$PYTHON" -c "import flask, flask_cors, flask_socketio, eventlet, requests" &>/dev/null; then
    echo "   Installing backend dependencies..."
    "$PYTHON" -m pip install -q -r "$SCRIPT_DIR/backend/requirements.txt"
fi

# Start Caddy reverse proxy (enables port 80 access by IP, e.g. Tailscale)
echo "   Starting Caddy reverse proxy on port 80..."
if command -v docker &>/dev/null; then
    docker compose -f "$SCRIPT_DIR/local-proxy/docker-compose.yml" up -d
else
    echo "   ⚠️  docker not found — skipping Caddy (port 80 unavailable, use :3325 directly)"
fi

# Start the backend API server
echo "   Starting backend API on port 5050..."
cd "$SCRIPT_DIR/backend"
$PYTHON app.py > /tmp/flightdeck-backend.log 2>&1 &
BACKEND_PID=$!

# Verify backend came up
sleep 3
if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "❌ Backend failed to start. Last error:"
    tail -20 /tmp/flightdeck-backend.log
    exit 1
fi
echo "   ✓ Backend running (PID $BACKEND_PID)"

# Start the homepage server
echo "   Starting homepage server on port 3325..."
cd "$SCRIPT_DIR/homepage"
$PYTHON server.py > /tmp/flightdeck-homepage.log 2>&1 &
HOMEPAGE_PID=$!

sleep 1
if ! kill -0 "$HOMEPAGE_PID" 2>/dev/null; then
    echo "❌ Homepage server failed to start. Last error:"
    tail -10 /tmp/flightdeck-homepage.log
    exit 1
fi
echo "   ✓ Homepage running (PID $HOMEPAGE_PID)"

# Open browser unless suppressed (e.g. when launched by launchd at login)
if [[ "${FLIGHTDECK_NO_BROWSER:-0}" != "1" ]]; then
    open "http://localhost:3325"
fi

echo ""
echo "✅ Flight Deck is running!"
echo "   Homepage:    http://localhost:3325"
echo "   Backend API: http://localhost:5050"
echo "   Logs:        /tmp/flightdeck-backend.log  /tmp/flightdeck-homepage.log"
echo ""
echo "   Press Ctrl+C to stop all services"

cleanup() {
    echo ""
    echo "🛬 Shutting down Flight Deck..."
    kill $BACKEND_PID 2>/dev/null
    kill $HOMEPAGE_PID 2>/dev/null
    exit 0
}

trap cleanup SIGINT SIGTERM

wait
