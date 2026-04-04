#!/bin/bash
# Entrypoint script for DeepIsaHOL Docker container
# Starts the Py4J gateway and then the FastAPI server

set -e

# Configuration
PY4J_PORT=${PY4J_PORT:-25333}
API_PORT=${API_PORT:-8000}
STARTUP_WAIT=${STARTUP_WAIT:-30}

# Signal handling for graceful shutdown
cleanup() {
    echo "Received shutdown signal, cleaning up..."

    # Kill the uvicorn process if running
    if [ -n "$UVICORN_PID" ]; then
        echo "Stopping uvicorn (PID: $UVICORN_PID)..."
        kill -TERM "$UVICORN_PID" 2>/dev/null || true
        wait "$UVICORN_PID" 2>/dev/null || true
    fi

    # Kill the sbt/gateway process if running
    if [ -n "$GATEWAY_PID" ]; then
        echo "Stopping Py4J gateway (PID: $GATEWAY_PID)..."
        kill -TERM "$GATEWAY_PID" 2>/dev/null || true
        wait "$GATEWAY_PID" 2>/dev/null || true
    fi

    echo "Cleanup complete"
    exit 0
}

trap cleanup SIGTERM SIGINT SIGQUIT

echo "=========================================="
echo "  DeepIsaHOL Docker Container Starting"
echo "=========================================="
echo ""
echo "Configuration:"
echo "  - Py4J Gateway Port: $PY4J_PORT"
echo "  - API Port: $API_PORT"
echo "  - Startup Wait: ${STARTUP_WAIT}s"
echo ""

# Change to the app directory
cd /app

# Start the Py4J gateway in the background
echo "Starting Py4J gateway on port $PY4J_PORT..."
sbt "runMain isabelle_rl.Py4j_Gateway_Main $PY4J_PORT" &
GATEWAY_PID=$!

# Wait for Isabelle initialization
# Isabelle takes a significant time to initialize on first use
echo "Waiting ${STARTUP_WAIT}s for Isabelle to initialize..."
sleep "$STARTUP_WAIT"

# Check if the gateway is still running
if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
    echo "ERROR: Py4J gateway process died during startup"
    exit 1
fi

# Check if ports.json exists (indicates gateway is ready)
MAX_WAIT=60
WAITED=0
while [ ! -f /app/ports.json ] && [ $WAITED -lt $MAX_WAIT ]; do
    echo "Waiting for gateway to register port..."
    sleep 5
    WAITED=$((WAITED + 5))
done

if [ ! -f /app/ports.json ]; then
    echo "WARNING: ports.json not found after ${MAX_WAIT}s, but continuing..."
fi

echo ""
echo "Starting FastAPI server on port $API_PORT..."
cd /app/docker
uvicorn api:app --host 0.0.0.0 --port "$API_PORT" &
UVICORN_PID=$!

echo ""
echo "=========================================="
echo "  DeepIsaHOL API is ready!"
echo "  http://localhost:$API_PORT"
echo "=========================================="
echo ""

# Wait for either process to exit
wait -n $GATEWAY_PID $UVICORN_PID

# If we get here, one of the processes exited
echo "A process exited unexpectedly, shutting down..."
cleanup
