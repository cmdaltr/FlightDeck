#!/bin/bash
# FlightDeck Startup Script
# Starts both the backend API and homepage server

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🛩️  Starting Flight Deck..."

# Start the backend API server
echo "   Starting backend API on port 5000..."
cd "$SCRIPT_DIR/backend"
python app.py &
BACKEND_PID=$!

# Wait a moment for backend to initialize
sleep 2

# Start the homepage server
echo "   Starting homepage server on port 3325..."
cd "$SCRIPT_DIR/homepage"
python server.py &
HOMEPAGE_PID=$!

# Wait a moment then open browser
sleep 1
echo "   Opening Flight Deck in browser..."
open "http://localhost:3325"

echo ""
echo "✅ Flight Deck is running!"
echo "   Homepage: http://localhost:3325"
echo "   Backend API: http://localhost:5000"
echo ""
echo "   Press Ctrl+C to stop all services"

# Handle shutdown
cleanup() {
    echo ""
    echo "🛬 Shutting down Flight Deck..."
    kill $BACKEND_PID 2>/dev/null
    kill $HOMEPAGE_PID 2>/dev/null
    exit 0
}

trap cleanup SIGINT SIGTERM

# Keep script running
wait
