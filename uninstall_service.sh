#!/bin/bash
# PlexSyncer — systemd service uninstaller
# Removes the service only. Your configs, venv, and media files are untouched.

SERVICE_NAME="plexsyncer.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " PlexSyncer Uninstaller"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo "🛑 Stopping and disabling PlexSyncer..."
sudo systemctl stop "$SERVICE_NAME"    2>/dev/null || true
sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true

if [ -f "$SERVICE_PATH" ]; then
    echo "🗑️  Removing service file..."
    sudo rm -f "$SERVICE_PATH"
else
    echo "⚠️  Service file not found — may have already been removed."
fi

echo "🔄 Reloading systemd..."
sudo systemctl daemon-reload
sudo systemctl reset-failed 2>/dev/null || true

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ PlexSyncer service removed."
echo ""
echo "   Your configs, venv, and sync files were NOT deleted."
echo "   To reinstall: bash install_service.sh"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
