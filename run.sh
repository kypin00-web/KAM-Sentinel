#!/bin/bash
# Run KAM Sentinel dev server (macOS / Linux)
# Usage: ./run.sh [PORT]   (default: 5000)

cd "$(dirname "$0")"

PORT=${1:-5000}

if ! python3 -c "import flask, psutil" 2>/dev/null; then
    echo "  Run ./setup.sh first to install dependencies."
    exit 1
fi

mkdir -p backups logs profiles

echo ""
echo "  Starting KAM Sentinel at http://localhost:$PORT"
echo "  Close the browser tab or press Ctrl+C to stop."
echo ""

# Open browser after a short delay (optional)
(sleep 2.5 && open "http://localhost:$PORT" 2>/dev/null) &

python3 server.py "$PORT"
