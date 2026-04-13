#!/bin/bash

# Ensure the script is NOT run as root initially, so it grabs the correct user
if [ "$EUID" -eq 0 ]; then
  echo "❌ Please run this script as your normal user, not with sudo."
  echo "It will ask for your sudo password when needed."
  exit 1
fi

APP_DIR=$(pwd)
APP_USER=$USER
VENV_PATH="$APP_DIR/venv/bin/streamlit"
SERVICE_NAME="plexsyncer.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"

# Sanity check to make sure the venv exists before creating the service
if [ ! -f "$VENV_PATH" ]; then
    echo "❌ Streamlit executable not found at: $VENV_PATH"
    echo "Make sure you are running this from the PlexSyncer directory and the venv is built."
    exit 1
fi

echo "⚙️  Generating service file for user: $APP_USER at $APP_DIR..."

# Create the service file locally
cat <<EOF > $SERVICE_NAME
[Unit]
Description=PlexSyncer Streamlit UI
After=network.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_PATH run sync_ui.py --server.headless true
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "🔐 Installing service (requires sudo)..."
sudo mv $SERVICE_NAME $SERVICE_PATH
sudo chown root:root $SERVICE_PATH
sudo chmod 644 $SERVICE_PATH

echo "🚀 Enabling and starting PlexSyncer..."
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME
sudo systemctl start $SERVICE_NAME

echo "✅ Install complete! Checking status..."
sleep 2
sudo systemctl --no-pager status $SERVICE_NAME