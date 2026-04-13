#!/bin/bash

SERVICE_NAME="plexsyncer.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"

echo "🛑 Stopping and disabling PlexSyncer..."
sudo systemctl stop $SERVICE_NAME
sudo systemctl disable $SERVICE_NAME

if [ -f "$SERVICE_PATH" ]; then
    echo "🗑️  Removing service file..."
    sudo rm -f $SERVICE_PATH
else
    echo "⚠️  Service file not found. It may have already been removed."
fi

echo "🔄 Reloading systemd..."
sudo systemctl daemon-reload
sudo systemctl reset-failed

echo "✅ PlexSyncer service has been completely removed."