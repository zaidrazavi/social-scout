#!/usr/bin/env bash
# ==========================================================================
#  social-scout installer for macOS / Linux
#  Installs the Python dependencies needed for the Claude Desktop MCP server.
#  Run it with:   bash install.sh    (or ./install.sh after chmod +x)
# ==========================================================================
set -u
cd "$(dirname "$0")"

echo
echo "==========================================================="
echo "  social-scout  -  installing Python dependencies"
echo "==========================================================="
echo

# --- 1. Find a Python interpreter ----------------------------------------
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "[X] Python was not found on your system."
    echo
    echo "    Please install Python 3.10 or newer:"
    echo "      macOS:  brew install python   (or https://www.python.org/downloads/)"
    echo "      Linux:  use your package manager, e.g. sudo apt install python3 python3-pip"
    echo
    exit 1
fi

echo "[OK] Found $($PY --version 2>&1)"
echo

# --- 2. Install dependencies ---------------------------------------------
echo "Installing dependencies from requirements.txt ..."
echo "(this can take a few minutes the first time)"
echo
if ! "$PY" -m pip install -r requirements.txt --break-system-packages; then
    echo
    echo "[X] Dependency installation failed. Scroll up to read the error."
    echo "    A common fix is to update pip first:"
    echo "        $PY -m pip install --upgrade pip"
    echo
    exit 1
fi

# --- 3. Optional: the stealth browser ------------------------------------
echo
echo "-----------------------------------------------------------"
echo "  Optional: the stealth browser (about 73 MB from GitHub)"
echo "  Reads JavaScript-heavy pages, takes screenshots, scrapes"
echo "  many URLs at once, and runs Meta Ad Library / Pinterest /"
echo "  X profiles without opening Chrome. You can add it later."
echo "-----------------------------------------------------------"
STEALTH=""
read -r -p "Install the stealth browser now? [Y/n] " STEALTH || STEALTH="n"
case "$STEALTH" in
    [nN]|[nN][oO]) ;;
    *)
        if "$PY" scripts/obscura.py install; then
            echo "[OK] Stealth browser installed."
        else
            echo "[!] The stealth browser didn't install - everything else still works."
            echo "    Try again later with:  $PY scripts/obscura.py install"
        fi
        ;;
esac

echo
echo "==========================================================="
echo "  [OK] Install complete!"
echo "==========================================================="
echo
echo "  Next step: connect social-scout to the Claude Desktop App."
echo "  Open SETUP_GUIDE.md (in this folder) and follow \"Step 3:"
echo "  Configure Claude Desktop\". You will paste a small snippet"
echo "  that points Claude Desktop at this folder's mcp_server.py."
echo
echo "  This folder is:"
echo "      $(pwd)"
echo
