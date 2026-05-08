#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT=8000
source venv/bin/activate

# Kill anything already on the port
if lsof -ti tcp:"$PORT" &>/dev/null; then
    echo "Port $PORT in use — killing existing process."
    lsof -ti tcp:"$PORT" | xargs kill -9 2>/dev/null || true
    sleep 2
fi

# Start uvicorn in the background
uvicorn app:app --port "$PORT" &
SERVER_PID=$!

trap "echo ''; echo 'Shutting down...'; kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null || true" EXIT

# Wait for OUR server to be ready before opening the browser
for i in $(seq 1 15); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "Server failed to start — check output above."
        exit 1
    fi
    if curl -sf "http://localhost:$PORT/api/queue/status" &>/dev/null; then
        break
    fi
    sleep 1
done

open "http://localhost:$PORT"
echo "Resume Tailor running at http://localhost:$PORT"
echo "Press Ctrl+C to stop."

wait $SERVER_PID
