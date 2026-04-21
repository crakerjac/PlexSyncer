#!/bin/bash
# PlexSyncer — systemd service uninstaller
# Removes the UI service and (if installed) the webhook service.
# Your configs, venv, and media files are untouched.

UI_SERVICE_NAME="plexsyncer.service"
UI_SERVICE_PATH="/etc/systemd/system/$UI_SERVICE_NAME"

WEBHOOK_SERVICE_NAME="plexsyncer-webhook.service"
WEBHOOK_SERVICE_PATH="/etc/systemd/system/$WEBHOOK_SERVICE_NAME"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " PlexSyncer Uninstaller"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── UI service ─────────────────────────────────────────────────────────────────

echo "🛑 Stopping and disabling PlexSyncer UI..."
sudo systemctl stop    "$UI_SERVICE_NAME"    2>/dev/null || true
sudo systemctl disable "$UI_SERVICE_NAME"    2>/dev/null || true

if [ -f "$UI_SERVICE_PATH" ]; then
    echo "🗑️  Removing UI service file..."
    sudo rm -f "$UI_SERVICE_PATH"
else
    echo "⚠️  UI service file not found — may have already been removed."
fi

# ── Webhook service (only if installed) ───────────────────────────────────────

if [ -f "$WEBHOOK_SERVICE_PATH" ]; then
    echo "🛑 Stopping and disabling PlexSyncer webhook..."
    sudo systemctl stop    "$WEBHOOK_SERVICE_NAME" 2>/dev/null || true
    sudo systemctl disable "$WEBHOOK_SERVICE_NAME" 2>/dev/null || true
    echo "🗑️  Removing webhook service file..."
    sudo rm -f "$WEBHOOK_SERVICE_PATH"
else
    echo "   Webhook service not installed — skipping."
fi

# ── Reload systemd ─────────────────────────────────────────────────────────────

echo "🔄 Reloading systemd..."
sudo systemctl daemon-reload
sudo systemctl reset-failed 2>/dev/null || true

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ PlexSyncer services removed."
echo ""
echo "   Your configs, venv, and sync files were NOT deleted."
echo "   To reinstall: bash install_service.sh"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
