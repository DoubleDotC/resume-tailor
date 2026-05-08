#!/usr/bin/env bash
# check_log.sh — Hourly log monitor for batch_run.log
# Reads lines added since last check, extracts issues, appends to overnight_report.md

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$SCRIPT_DIR/data/batch_run.log"
REPORT="$SCRIPT_DIR/data/overnight_report.md"
CURSOR="$SCRIPT_DIR/data/.log_cursor"

# How many lines were in the log last time we checked
LAST_LINE=0
if [[ -f "$CURSOR" ]]; then
    LAST_LINE=$(cat "$CURSOR")
fi

CURRENT_LINES=$(wc -l < "$LOG" 2>/dev/null || echo 0)
echo "$CURRENT_LINES" > "$CURSOR"

# No new lines since last check
if [[ "$CURRENT_LINES" -le "$LAST_LINE" ]]; then
    exit 0
fi

# Extract new lines
NEW=$(tail -n +"$((LAST_LINE + 1))" "$LOG" 2>/dev/null)

TIMESTAMP=$(date '+%Y-%m-%d %H:%M')

# Count key events
COMPLETED=$(echo "$NEW" | grep -c "PDF created" || true)
ERRORS=$(echo "$NEW" | grep -c "ERROR\|error\|timed out\|exited unexpectedly" || true)
WARNINGS=$(echo "$NEW" | grep -c "WARNING\|parse failed\|Could not find" || true)
INSERTED=$(echo "$NEW" | grep "Inserted.*jobs into DB" | tail -1 || true)

# Build report section
{
    echo ""
    echo "## $TIMESTAMP"
    echo ""
    echo "**New lines:** $((CURRENT_LINES - LAST_LINE))  |  **Jobs completed:** $COMPLETED  |  **Errors:** $ERRORS  |  **Warnings:** $WARNINGS"
    if [[ -n "$INSERTED" ]]; then
        echo ""
        echo "- $INSERTED"
    fi

    # Print errors prominently
    if [[ "$ERRORS" -gt 0 ]]; then
        echo ""
        echo "### ⚠ Errors"
        echo '```'
        echo "$NEW" | grep -i "ERROR\|error\|timed out\|exited unexpectedly" | head -20
        echo '```'
    fi

    # Print warnings (skip routine ones)
    NOTABLE_WARNINGS=$(echo "$NEW" | grep "WARNING" | grep -v "Standard load failed\|Startup: reset\|parse failed" || true)
    if [[ -n "$NOTABLE_WARNINGS" ]]; then
        echo ""
        echo "### Notable warnings"
        echo '```'
        echo "$NOTABLE_WARNINGS" | head -20
        echo '```'
    fi

    # Show completed jobs
    if [[ "$COMPLETED" -gt 0 ]]; then
        echo ""
        echo "### Completed this hour"
        echo '```'
        echo "$NEW" | grep "PDF created" | sed 's|.*Resume_||; s|\.pdf.*||'
        echo '```'
    fi

    echo ""
    echo "---"
} >> "$REPORT"
