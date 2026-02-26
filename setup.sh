#!/bin/bash
# KAM Sentinel - First-time setup (macOS / Linux)

set -e
cd "$(dirname "$0")"

echo ""
echo "  ============================================"
echo "   KAM Sentinel - First Time Setup"
echo "  ============================================"
echo ""

# Check for Python 3
if ! command -v python3 &>/dev/null; then
    echo "  [ERROR] Python 3 not found!"
    echo "  Install with: brew install python3"
    echo ""
    exit 1
fi

echo "  [OK] Python found: $(python3 --version)"
echo "  [..] Installing required packages..."
echo ""

# Core deps (cross-platform). Skip wmi/pywin32 on non-Windows.
pip3 install --user flask psutil

# Optional: GPU monitoring (nvidia-smi on Linux; less relevant on macOS but harmless)
pip3 install --user GPUtil 2>/dev/null || echo "  [WARN] GPUtil optional - GPU may show N/A"

echo ""
echo "  [..] Creating directories..."
mkdir -p backups logs profiles
echo "  [OK] Directories created"
echo ""
echo "  ============================================"
echo "   Setup complete! Run: ./run.sh  or  python3 server.py"
echo "   Then open: http://localhost:5000"
echo "  ============================================"
echo ""
