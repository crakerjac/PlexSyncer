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

UI_SERVICE_NAME="plexsyncer.service"
UI_SERVICE_PATH="/etc/systemd/system/$UI_SERVICE_NAME"

WEBHOOK_SERVICE_NAME="plexsyncer-webhook.service"
WEBHOOK_SERVICE_PATH="/etc/systemd/system/$WEBHOOK_SERVICE_NAME"

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
    "$VENV_PIP" install streamlit plexapi requests flask waitress --quiet
fi
echo "✓ Dependencies installed"

# Verify streamlit is available
if [ ! -f "$VENV_STREAMLIT" ]; then
    echo "❌ Streamlit executable not found after install. Something went wrong."
    exit 1
fi
STREAMLIT_VERSION=$("$VENV_STREAMLIT" --version 2>&1)
echo "✓ $STREAMLIT_VERSION"

# ── 4. Create UI systemd service file ─────────────────────────────────────────
echo "⚙️  Generating UI service file..."
cat > "/tmp/$UI_SERVICE_NAME" << UNIT
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

echo "🔐 Installing UI service (requires sudo)..."
sudo mv "/tmp/$UI_SERVICE_NAME" "$UI_SERVICE_PATH"
sudo chown root:root "$UI_SERVICE_PATH"
sudo chmod 644 "$UI_SERVICE_PATH"

# ── 5. Enable and start UI service ─────────────────────────────────────────────
echo "🚀 Enabling and starting PlexSyncer UI..."
sudo systemctl daemon-reload
sudo systemctl enable "$UI_SERVICE_NAME"
sudo systemctl start "$UI_SERVICE_NAME"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ PlexSyncer UI installed and running"
echo "   UI available at: http://localhost:8501"
echo "   LAN access at:   http://$(hostname -I | awk '{print $1}'):8501"
echo ""
echo "   Manage with:"
echo "     sudo systemctl status $UI_SERVICE_NAME"
echo "     sudo systemctl restart $UI_SERVICE_NAME"
echo "     sudo journalctl -u $UI_SERVICE_NAME -f"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

sleep 2
sudo systemctl --no-pager status "$UI_SERVICE_NAME"

# ── 6. Optional webhook service ───────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Optional: Plex Webhook"
echo ""
echo " The webhook receiver triggers --all-slots immediately when Plex"
echo " marks an item as watched, keeping your sync manifests current"
echo " without waiting for the next cron run."
echo ""
read -r -p " Install the webhook service? [y/N] " INSTALL_WEBHOOK
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [[ "$INSTALL_WEBHOOK" =~ ^[Yy]$ ]]; then

    if [ ! -f "$APP_DIR/plex_webhook.py" ]; then
        echo "❌ plex_webhook.py not found in $APP_DIR"
        echo "   Skipping webhook installation."
    else
        echo "⚙️  Generating webhook service file..."
        cat > "/tmp/$WEBHOOK_SERVICE_NAME" << UNIT
[Unit]
Description=PlexSyncer Webhook Receiver
After=network.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_PYTHON $APP_DIR/plex_webhook.py
Restart=on-failure
RestartSec=5
Environment="PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=multi-user.target
UNIT

        echo "🔐 Installing webhook service (requires sudo)..."
        sudo mv "/tmp/$WEBHOOK_SERVICE_NAME" "$WEBHOOK_SERVICE_PATH"
        sudo chown root:root "$WEBHOOK_SERVICE_PATH"
        sudo chmod 644 "$WEBHOOK_SERVICE_PATH"

        echo "🚀 Enabling and starting PlexSyncer webhook..."
        sudo systemctl daemon-reload
        sudo systemctl enable "$WEBHOOK_SERVICE_NAME"
        sudo systemctl start "$WEBHOOK_SERVICE_NAME"

        SERVER_IP=$(hostname -I | awk '{print $1}')
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "✅ Webhook service installed and running"
        echo "   Listening on port 5001"
        echo ""
        echo "   Configure in Plex:"
        echo "   Settings → Webhooks → Add Webhook"
        echo "   http://localhost:5001/plexhook"
        echo ""
        echo "   (localhost works because PlexSyncer runs on the same"
        echo "    machine as Plex — no firewall changes needed)"
        echo ""
        echo "   Manage with:"
        echo "   sudo systemctl status $WEBHOOK_SERVICE_NAME"
        echo "   sudo systemctl restart $WEBHOOK_SERVICE_NAME"
        echo "   sudo journalctl -u $WEBHOOK_SERVICE_NAME -f"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""

        sleep 2
        sudo systemctl --no-pager status "$WEBHOOK_SERVICE_NAME"
    fi

else
    echo ""
    echo "   Webhook skipped. You can install it later by running:"
    echo "   bash install_service.sh"
    echo "   (or install manually — see README §16)"
fi
