#!/bin/bash
# Start Lens Inspector, and keep it started. Invoked by the @reboot crontab entry, and safe
# to run by hand -- it is a no-op if the app is already listening.
#
# SECRET_KEY lives in .env rather than being generated per start -- otherwise every
# restart signs out every grader.
#
# Set APP_DIR to wherever you cloned this, then keep it running with cron:
#   @reboot        /path/to/start_inspector.sh
#   */10 * * * *   /path/to/start_inspector.sh   # watchdog; a no-op if already up
set -u
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$APP_DIR/.venv/bin/python"
LOG="$APP_DIR/data/inspector_server.log"

cd "$APP_DIR" || exit 1

if [ -f .env ]; then set -a; . ./.env; set +a; fi

# already serving? then there is nothing to do (cron reruns, manual reruns, double @reboot)
if curl -s -o /dev/null --max-time 5 "http://127.0.0.1:${PORT:-8000}/" 2>/dev/null; then
    echo "$(date -Is) already listening on ${PORT:-8000}, nothing to do" >> "$LOG"
    exit 0
fi

echo "$(date -Is) starting lens inspector" >> "$LOG"
exec "$PY" run_inspector.py >> "$LOG" 2>&1
