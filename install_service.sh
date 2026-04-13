#!/bin/bash
# PlexSyncer — systemd service installer
# Run as your normal user (not root). sudo will be invoked as needed.

set -e

if [ "$EUID" -eq 0 ]; then
    echo "❌ Please run as your normal user, not with sudo."
    echo "   The script will ask for your password when needed."
    exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER="$USER"
VENV_DIR="$APP_DIR/venv"
PYTHON_BIN="$(which python3)"
SERVICE_NAME="plexsyncer.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
REQUIREMENTS="$APP_DIR/requirements.txt"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " PlexSyncer Installer"
echo " Directory : $APP_DIR"
echo " User      : $APP_USER"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 1. Python check ────────────────────────────────────────────────────────────
if [ -z "$PYTHON_BIN" ]; then
    echo "❌ python3 not found. Install it and try again."
    exit 1
fi
PYTHON_VERSION=$("$PYTHON_BIN" --version 2>&1)
echo "✓ Found $PYTHON_VERSION"

# ── 2. Virtual environment ─────────────────────────────────────────────────────
if [ -d "$VENV_DIR" ]; then
    echo "✓ Virtual environment already exists at $VENV_DIR"
else
    echo "⚙️  Creating virtual environment..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    echo "✓ Virtual environment created"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"
VENV_STREAMLIT="$VENV_DIR/bin/streamlit"

# ── 3. Install dependencies ────────────────────────────────────────────────────
echo "⚙️  Installing / upgrading dependencies..."
"$VENV_PIP" install --upgrade pip --quiet

if [ -f "$REQUIREMENTS" ]; then
    echo "   Installing from requirements.txt..."
    "$VENV_PIP" install -r "$REQUIREMENTS" --quiet
else
    echo "   No requirements.txt found. Installing defaults..."
    "$VENV_PIP" install streamlit plexapi requests --quiet
fi
echo "✓ Dependencies installed"

# Verify streamlit is available
if [ ! -f "$VENV_STREAMLIT" ]; then
    echo "❌ Streamlit executable not found after install. Something went wrong."
    exit 1
fi
STREAMLIT_VERSION=$("$VENV_STREAMLIT" --version 2>&1)
echo "✓ $STREAMLIT_VERSION"

# ── 4. Create systemd service file ────────────────────────────────────────────
echo "⚙️  Generating service file..."
cat > "/tmp/$SERVICE_NAME" << UNIT
[Unit]
Description=PlexSyncer Streamlit UI
After=network.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_STREAMLIT run sync_ui.py --server.headless true --server.port 8501
Restart=on-failure
RestartSec=5
Environment="PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=multi-user.target
UNIT

echo "🔐 Installing service (requires sudo)..."
sudo mv "/tmp/$SERVICE_NAME" "$SERVICE_PATH"
sudo chown root:root "$SERVICE_PATH"
sudo chmod 644 "$SERVICE_PATH"

# ── 5. Enable and start ────────────────────────────────────────────────────────
echo "🚀 Enabling and starting PlexSyncer..."
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ PlexSyncer installed and running"
echo "   UI available at: http://localhost:8501"
echo "   LAN access at:   http://$(hostname -I | awk '{print $1}'):8501"
echo ""
echo "   Manage with:"
echo "     sudo systemctl status $SERVICE_NAME"
echo "     sudo systemctl restart $SERVICE_NAME"
echo "     sudo journalctl -u $SERVICE_NAME -f"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

sleep 2
sudo systemctl --no-pager status "$SERVICE_NAME"
