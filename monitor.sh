#!/usr/bin/env bash
# monitor.sh — Active pipeline monitor: detects stalls, fixes them, restarts queue
# Run via cron every 15 minutes. Safe to run concurrently (lock file protected).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG="$SCRIPT_DIR/data/batch_run.log"
REPORT="$SCRIPT_DIR/data/overnight_report.md"
CURSOR="$SCRIPT_DIR/data/.log_cursor"
LOCKDIR="$SCRIPT_DIR/data/.monitor.lock"
PORT=8000
STALL_MINUTES=20   # minutes a job can be 'generating' before it's considered stuck
DB="$SCRIPT_DIR/data/applications.db"

# ── Lock: prevent overlapping runs (mkdir is atomic on macOS) ────────────────
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    STORED_PID=$(cat "$LOCKDIR/pid" 2>/dev/null)
    if [[ -n "$STORED_PID" ]] && kill -0 "$STORED_PID" 2>/dev/null; then
        exit 0  # still running
    fi
    rm -rf "$LOCKDIR" && mkdir "$LOCKDIR"  # stale lock — clear it
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT

TIMESTAMP=$(date '+%Y-%m-%d %H:%M')

append_report() { echo -e "$1" >> "$REPORT"; }

# ── 1. Check if server is alive ──────────────────────────────────────────────
SERVER_UP=false
if curl -sf "http://localhost:$PORT/api/queue/status" &>/dev/null; then
    SERVER_UP=true
fi

if ! $SERVER_UP; then
    append_report ""
    append_report "## $TIMESTAMP  ⚠ SERVER DOWN — restarting"
    append_report ""

    # Kill any orphaned workers first
    pkill -f "multiprocessing.spawn import spawn_main" 2>/dev/null || true
    sleep 1

    source venv/bin/activate
    uvicorn app:app --port "$PORT" >> "$LOG" 2>&1 &

    for i in $(seq 1 15); do
        sleep 1
        curl -sf "http://localhost:$PORT/api/queue/status" &>/dev/null && break
    done

    if curl -sf "http://localhost:$PORT/api/queue/status" &>/dev/null; then
        append_report "Server restarted successfully."
        SERVER_UP=true
    else
        append_report "**FAILED to restart server — manual intervention needed.**"
        exit 1
    fi
fi

# ── 2. Check for stuck 'generating' jobs ────────────────────────────────────
STUCK=$(sqlite3 "$DB" \
    "SELECT id, company FROM applications
     WHERE status='generating'
     AND (julianday('now') - julianday(updated_at)) * 1440 > $STALL_MINUTES
     LIMIT 1;" 2>/dev/null)

if [[ -n "$STUCK" ]]; then
    STUCK_ID=$(echo "$STUCK" | cut -d'|' -f1)
    STUCK_CO=$(echo "$STUCK" | cut -d'|' -f2)

    append_report ""
    append_report "## $TIMESTAMP  ⚠ Stuck job detected — $STUCK_CO"
    append_report ""
    append_report "Job \`${STUCK_ID:0:8}\` stuck in \`generating\` for >${STALL_MINUTES} min. Killing worker and marking error."
    append_report ""

    # Kill worker subprocess
    pkill -f "multiprocessing.spawn import spawn_main" 2>/dev/null || true
    sleep 2

    # Mark job as error
    sqlite3 "$DB" \
        "UPDATE applications SET status='error', updated_at=datetime('now')
         WHERE id='$STUCK_ID';" 2>/dev/null

    # Reset any other stuck generating rows
    RESET_COUNT=$(sqlite3 "$DB" \
        "UPDATE applications SET status='queued', updated_at=datetime('now')
         WHERE status='generating';
         SELECT changes();" 2>/dev/null | tail -1)

    [[ "$RESET_COUNT" -gt 0 ]] && append_report "Reset $RESET_COUNT other stuck row(s) to queued."

    # Restart queue
    sleep 2
    START=$(curl -sf -X POST "http://localhost:$PORT/api/queue/start" 2>/dev/null)
    STARTED=$(echo "$START" | python3 -c "import sys,json; print(json.load(sys.stdin).get('started','?'))" 2>/dev/null)
    QUEUED=$(echo "$START"  | python3 -c "import sys,json; print(json.load(sys.stdin).get('queued','?'))" 2>/dev/null)

    append_report "Queue restarted: started=$STARTED, remaining=$QUEUED jobs."
    exit 0
fi

# ── 3. Check if queue has jobs but nothing is running (idle stall) ───────────
STATUS=$(curl -sf "http://localhost:$PORT/api/queue/status" 2>/dev/null)
QUEUED=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('queued',0))" 2>/dev/null || echo 0)
ACTIVE=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('active_id') or '')" 2>/dev/null || echo "")

if [[ "$QUEUED" -gt 0 && -z "$ACTIVE" ]]; then
    # Check if queue has been idle > STALL_MINUTES (last done job timestamp)
    LAST_DONE=$(sqlite3 "$DB" \
        "SELECT updated_at FROM applications WHERE status='done'
         ORDER BY updated_at DESC LIMIT 1;" 2>/dev/null)
    IDLE_MIN=$(python3 -c "
from datetime import datetime, timezone
import sys
ts = '$LAST_DONE'
if not ts: sys.exit(0)
try:
    dt = datetime.fromisoformat(ts)
    now = datetime.now(timezone.utc)
    print(int((now - dt).total_seconds() / 60))
except: print(0)
" 2>/dev/null || echo 0)

    if [[ "$IDLE_MIN" -gt "$STALL_MINUTES" ]]; then
        append_report ""
        append_report "## $TIMESTAMP  ⚠ Queue idle for ${IDLE_MIN} min with $QUEUED jobs waiting — restarting"
        append_report ""

        START=$(curl -sf -X POST "http://localhost:$PORT/api/queue/start" 2>/dev/null)
        STARTED=$(echo "$START" | python3 -c "import sys,json; print(json.load(sys.stdin).get('started','?'))" 2>/dev/null)
        append_report "Queue kick: started=$STARTED, $QUEUED jobs remaining."
        exit 0
    fi
fi

# ── 4. Log progress since last check (hourly summary) ───────────────────────
LAST_LINE=0
[[ -f "$CURSOR" ]] && LAST_LINE=$(cat "$CURSOR")
CURRENT_LINES=$(wc -l < "$LOG" 2>/dev/null || echo 0)
echo "$CURRENT_LINES" > "$CURSOR"

if [[ "$CURRENT_LINES" -gt "$LAST_LINE" ]]; then
    NEW=$(tail -n +"$((LAST_LINE + 1))" "$LOG" 2>/dev/null)
    COMPLETED=$(echo "$NEW" | grep -c "PDF created" || true)
    ERRORS=$(echo "$NEW" | grep -c "ERROR\|timed out\|exited unexpectedly" || true)
    PARSE_FAILS=$(echo "$NEW" | grep -c "parse failed\|decode error\|validation error" || true)

    # Only write to report if something notable happened
    if [[ "$COMPLETED" -gt 0 || "$ERRORS" -gt 0 ]]; then
        append_report ""
        append_report "## $TIMESTAMP"
        append_report ""
        append_report "**Completed:** $COMPLETED  |  **Errors:** $ERRORS  |  **Parse retries:** $PARSE_FAILS  |  **Queue remaining:** $QUEUED"

        if [[ "$COMPLETED" -gt 0 ]]; then
            append_report ""
            append_report "\`\`\`"
            echo "$NEW" | grep "PDF created" | sed 's|.*Resume_||; s|\.pdf.*||'
            append_report "\`\`\`"
        fi

        if [[ "$ERRORS" -gt 0 ]]; then
            append_report ""
            append_report "### Errors"
            append_report "\`\`\`"
            echo "$NEW" | grep -i "ERROR\|timed out\|exited unexpectedly" | head -10 >> "$REPORT"
            append_report "\`\`\`"
        fi

        append_report ""
        append_report "---"
    fi
fi
