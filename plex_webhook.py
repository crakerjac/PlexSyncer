"""
plex_webhook.py — PlexSyncer Webhook Receiver

Listens for Plex webhook events and triggers plex_hardlink_sync.py --all-slots
when a media.scrobble event is received (i.e. an item is marked as watched).

This keeps slot manifests current without waiting for the next cron run —
useful when a device syncs via rclone shortly after watching something.

flock(1) ensures that if a sync is already running (via cron or a previous
webhook), the new invocation fails silently rather than stacking.

Setup:
  1. Run install_service.sh and opt in to the webhook service, or:
       pip install flask waitress
       python plex_webhook.py
  2. In Plex: Settings → Webhooks → Add Webhook
       http://localhost:5001/plexhook

Runs on port 5001 by default (Streamlit UI is on 8501).
Includes a 5-minute rate limit.
"""

import json
import logging
import os
import subprocess
import time
from flask import Flask, request

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(SCRIPT_DIR, 'venv', 'bin', 'python')
WORKER      = os.path.join(SCRIPT_DIR, 'plex_hardlink_sync.py')
LOCK_FILE   = '/tmp/plexsyncer.lock'
TIME_FILE   = '/tmp/plexsyncer_last_run.txt'
PORT        = 5001

# Cooldown in seconds (5 minutes)
COOLDOWN_SECONDS = 300 

# ── App ────────────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route('/plexhook', methods=['POST'])
def plexhook():
    # Plex sends webhook data as multipart/form-data with a 'payload' field
    payload_str = request.form.get('payload')
    if not payload_str:
        log.warning('Received request with no payload')
        return 'No payload', 400

    try:
        data = json.loads(payload_str)
    except json.JSONDecodeError:
        log.warning('Received request with invalid JSON payload')
        return 'Invalid JSON', 400

    event = data.get('event', '')
    title = data.get('Metadata', {}).get('title', 'unknown')

    log.info(f'Received webhook event: {event}')

    # 1. Filter for media.stop
    if event != 'media.stop':
        return 'OK', 200

    # 2. Check the rate limit
    current_time = time.time()
    last_run = 0.0

    if os.path.exists(TIME_FILE):
        try:
            with open(TIME_FILE, 'r') as f:
                last_run = float(f.read().strip())
        except ValueError:
            pass # If the file is corrupted, default to 0.0 and allow the run

    time_since_last_run = current_time - last_run

    if time_since_last_run < COOLDOWN_SECONDS:
        time_left = int(COOLDOWN_SECONDS - time_since_last_run)
        log.info(f'Rate limit active. Skipping sync for "{title}". Cooldown ends in {time_left}s.')
        return 'OK', 200 # Return 200 so Plex doesn't think the webhook failed

    # 3. Update the timestamp for the next run
    with open(TIME_FILE, 'w') as f:
        f.write(str(current_time))

    log.info(f'Received event [{event}] for "{title}" — triggering sync')

    # Fire-and-forget: return 200 immediately, sync runs in background.
    # flock -n acquires a non-blocking lock; if a sync is already running
    # (via cron or a previous webhook), this invocation exits silently.
    subprocess.Popen(
        [
            '/usr/bin/flock', '-n', LOCK_FILE,
            VENV_PYTHON, WORKER, '--all-slots',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return 'OK', 200

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    try:
        from waitress import serve
        log.info(f'PlexSyncer webhook listening on port {PORT}')
        log.info(f'Configure Plex webhook URL: http://localhost:{PORT}/plexhook')
        serve(app, host='0.0.0.0', port=PORT, threads=4)
    except ImportError:
        log.warning('waitress not installed — using Flask dev server')
        log.warning('Install waitress: pip install waitress')
        app.run(host='0.0.0.0', port=PORT)
