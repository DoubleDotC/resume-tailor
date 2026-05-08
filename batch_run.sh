#!/usr/bin/env bash
# batch_run.sh — Overnight batch: scrape jobs → process queue
#
# Usage:
#   ./batch_run.sh                    # scrape LinkedIn + process queue
#   ./batch_run.sh --dry-run          # preview scraper output only, no DB writes
#   ./batch_run.sh --no-scrape        # skip scraping, process existing queue
#   ./batch_run.sh --no-pdf           # skip PDF generation (faster)
#
# When done, run ./start.sh to review results in the dashboard.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_FILE="data/batch_run.log"
mkdir -p data

# ── Colours ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}▸ $*${RESET}"; }
success() { echo -e "${GREEN}✓ $*${RESET}"; }
warn()    { echo -e "${YELLOW}⚠ $*${RESET}"; }
header()  { echo -e "\n${BOLD}$*${RESET}"; }

source venv/bin/activate

# ── Parse flags ───────────────────────────────────────────────────────────────
DRY_RUN=false
NO_SCRAPE=false
SCRAPER_EXTRA=()
QUEUE_EXTRA=()

for arg in "$@"; do
    case "$arg" in
        --dry-run)   DRY_RUN=true;  SCRAPER_EXTRA+=("$arg") ;;
        --no-scrape) NO_SCRAPE=true ;;
        --no-pdf)    QUEUE_EXTRA+=("$arg") ;;
        *)           SCRAPER_EXTRA+=("$arg") ;;
    esac
done

# ─────────────────────────────────────────────────────────────────────────────
header "━━━  Resume Tailor Batch Run  ━━━"
echo -e "  Log: ${CYAN}$LOG_FILE${RESET}"

# ─────────────────────────────────────────────────────────────────────────────
if ! $NO_SCRAPE; then
    header "STEP 1 / 2  —  Scraping LinkedIn"
    info "Running scraper (browser window will open — log in if prompted)..."
    echo ""

    python3 scraper.py "${SCRAPER_EXTRA[@]+"${SCRAPER_EXTRA[@]}"}" 2>&1 | tee -a "$LOG_FILE"
    SCRAPE_EXIT=${PIPESTATUS[0]}
    echo ""

    if [[ $SCRAPE_EXIT -ne 0 ]]; then
        warn "Scraper exited with code $SCRAPE_EXIT — check output above."
        warn "Continuing with any jobs already in the queue."
    fi
fi

if $DRY_RUN; then
    success "Dry run complete — no jobs inserted, nothing processed."
    exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
STEP_LABEL=$( $NO_SCRAPE && echo "1 / 1" || echo "2 / 2" )
header "STEP $STEP_LABEL  —  Processing queue"
info "Starting queue processor..."
echo ""

python3 process_queue.py --log "$LOG_FILE" "${QUEUE_EXTRA[@]+"${QUEUE_EXTRA[@]}"}"

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}${BOLD}  Batch run complete.${RESET}"
echo -e "  Run ${CYAN}./start.sh${RESET} to review results in the dashboard."
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
