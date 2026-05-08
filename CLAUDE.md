# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

A resume tailoring tool that uses local LLMs (MLX, Apple Silicon only) to adapt a master resume to job descriptions at scale. The full workflow:

1. **Scraper** finds jobs on LinkedIn → inserts them as `status=queued`
2. **Queue processor** drains the queue one job at a time through the 5-pass MLX pipeline (no web server needed)
3. **Dashboard** shows scored resumes — one-click Apply opens the job URL and marks it applied

## Directory Structure

```
resume_tailor/
├── app.py                    # FastAPI web server (dashboard + API)
├── process_queue.py          # Standalone queue processor (no server needed)
├── scraper.py                # LinkedIn job scraper
├── scraper_config.json       # Search preferences, exclusion rules, platform config
├── scraper_config_test.json  # Minimal test config (1 location, 1 role, 5 jobs)
├── mlx_resume_v4.py          # 5-pass MLX pipeline (core engine)
├── batch_run.sh              # Overnight batch: scrape → process queue
├── start.sh                  # Launch dashboard + open browser
├── master_resume.md          # Master resume — edit this
├── job_description.txt       # Scratch JD for CLI runs
├── resume_template.tex       # LaTeX template for PDF output
├── static/index.html         # Single-page web UI (Alpine.js + Tailwind)
├── data/
│   ├── applications.db       # SQLite tracker (auto-created, WAL mode)
│   ├── batch_run.log         # Batch run output log
│   └── applications/         # Per-application output files (md, pdf, debug/)
├── skills/                   # Reference blueprints (read-only)
└── venv/                     # Python environment
```

## One-Time Setup

```bash
source venv/bin/activate
pip install playwright beautifulsoup4
playwright install chromium
```

PDF generation also requires system tools: `pandoc` and `xelatex`.

## Full Workflow (recommended)

### Option A — Overnight batch (hands-off)

```bash
./batch_run.sh                    # scrape LinkedIn + process all queued jobs
./batch_run.sh --no-scrape        # skip scraping, process existing queue only
./batch_run.sh --dry-run          # preview scraper output, no processing
./batch_run.sh --no-pdf           # skip PDF generation (faster)
```

When done, open the dashboard to review: `./start.sh`

### Option B — Step by step

#### Step 1 — Scrape jobs

```bash
source venv/bin/activate

# Preview without inserting (always do this first)
python3 scraper.py --dry-run

# LinkedIn scrape (uses your existing Chrome session)
python3 scraper.py

# Use minimal test config (1 location, 1 role, 5 jobs max)
python3 scraper.py --config scraper_config_test.json
```

LinkedIn first run: opens a visible browser window — log in once, then subsequent runs use `headless=True`.

#### Step 2 — Process queue

```bash
python3 process_queue.py                         # process all queued jobs
python3 process_queue.py --log data/batch.log    # also write to log file
python3 process_queue.py --no-pdf                # skip PDF generation
python3 process_queue.py --dry-run               # show queue count only
```

No web server needed. Spawns one worker subprocess per job, shows live pass-by-pass progress, prints session summary at the end.

#### Step 3 — Review and apply

```bash
./start.sh
# Opens http://localhost:8000
```

Dashboard → filter by "Done" → expand row → click green **Apply** button → opens job URL in browser, marks as applied.

## Running the CLI directly (single job)

```bash
source venv/bin/activate
python3 mlx_resume_v4.py <output_name> --master master_resume.md --job job_description.txt
python3 mlx_resume_v4.py <output_name> --no-pdf           # faster
python3 mlx_resume_v4.py <output_name> --min-score 80     # stricter threshold
python3 mlx_resume_v4.py <output_name> --no-inference     # skip semantic inference pass
```

## scraper_config.json

LinkedIn-only configuration. Key fields to customise:
- `locations` — per-platform location list
- `categories` — search query groups (edit to add/remove job types)
- `exclude_companies` — company blacklist (banks etc.)
- `exclude_title_keywords` — title keyword blacklist
- `max_per_platform` — cap per scraper run (default 200 for LinkedIn)
- `max_per_query` — max JD fetches per query-location combination
- `linkedin_user_data_dir` — path to Chrome profile for LinkedIn auth

## Architecture of `process_queue.py`

Standalone queue processor — the primary way to run batch jobs:
- **Direct SQLite access** — no web server dependency, no port conflicts
- **Worker subprocess** per job (`multiprocessing` spawn context) — all MLX/Metal memory freed when subprocess exits
- **Crash recovery** — resets any `generating` rows to `queued` on startup
- **Graceful Ctrl+C** — current job resets to `queued`, remaining jobs untouched
- **15-minute timeout** per job — stuck MLX runs are killed and marked `error`
- **Session summary** — total done/errors, average score, duration

## Architecture of `app.py`

FastAPI backend (dashboard + API only for interactive use):
- **SQLite** at `data/applications.db` (WAL mode, safe for concurrent reads)
- **Pipeline subprocess** (`multiprocessing` spawn context) — all MLX/Metal memory is freed when the subprocess exits; uvicorn stays lean between runs
- **SSE streaming** — live pipeline progress pushed to the browser
- **`pipeline_lock`** (`threading.Lock`, not RLock) — one concurrent run enforced; acquired in HTTP handler thread, released in executor thread
- **Auto-drain** — `_start_next_queued()` called in the `finally` block after each run; processes the queue fully unattended
- **SIGTERM + atexit handlers** — worker subprocess is always terminated cleanly on server shutdown
- **15-minute job timeout** — stuck MLX runs are killed and marked `error` automatically
- **Startup reset** — any `generating` rows left from a crash are reset to `queued` on next start
- **DB migration** — `gap_score` column added automatically on startup for older DBs

## Architecture of `mlx_resume_v4.py`

Five-pass LLM pipeline:

1. **Pass 1 · Analyzer** — extracts ATS keywords, required/preferred skills, responsibilities, company signals
2. **Pass 1b · Inference** — semantic inference on blocked keywords (inferable vs. truly missing)
3. **Pass 2 · Writer** — rewrites resume with 3-tier keyword system
4. **Pass 3 · Validator** — scores ATS coverage + bullet quality, fabrication check
5. **Pass 4 · Corrector** — sparse patch for weak bullets/missing keywords; skipped if score ≥ threshold

Default model: `mlx-community/Qwen3.5-9B-MLX-4bit`. Runs under 5 min on M4 MacBook Air 16GB.

## Architecture of `scraper.py`

LinkedIn-only scraper:
- `LinkedInScraper` — Playwright `launch_persistent_context` (reuses Chrome session); auto-dismisses modals
- `RelevanceFilter` — keyword overlap between JD and master resume vocab (6% threshold on JD-side)
- `FilterEngine` — company + title exclusions, JD length check
- `DeduplicatorDB` — URL normalisation against existing DB rows
- `ScraperOrchestrator` — drives scraper → filter → dedup → insert as `status=queued`

## Dependencies

All in venv: `mlx-lm`, `fastapi`, `uvicorn[standard]`, `pydantic`, `jinja2`, `playwright`, `beautifulsoup4`

System: `pandoc`, `xelatex`
