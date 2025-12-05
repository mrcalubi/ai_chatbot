#!/bin/bash
# Run server with visible output for debugging

cd "$(dirname "$0")"
source .venv/bin/activate

echo "Starting server on port 8001..."
echo "Logs will be visible in this terminal"
echo "Press Ctrl+C to stop"
echo ""

uvicorn app.main:app --host 0.0.0.0 --port 8001 --log-level info

