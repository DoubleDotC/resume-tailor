# resume-tailor

A local resume-tailoring tool for Apple Silicon Macs. It scrapes LinkedIn for
relevant jobs, then runs each job description through a 5-pass MLX (local LLM)
pipeline that adapts a master resume to the role and scores it against ATS
keywords. A small FastAPI dashboard surfaces the results — one click opens the
job posting and marks it applied.

Everything runs locally. No API keys required. Default model is a 9B Qwen
quantized to 4-bit MLX; full pipeline runs in under 5 minutes on an M-series
MacBook Air with 16 GB RAM.

## Requirements

- macOS on Apple Silicon (M1 / M2 / M3 / M4) — MLX is Apple-only
- Python 3.10+
- `pandoc` and `xelatex` (for PDF generation)
- Chrome installed (LinkedIn scraper reuses your existing session)

```bash
brew install pandoc
brew install --cask mactex   # provides xelatex (large; or `brew install basictex`)
```

## One-time setup

```bash
git clone https://github.com/DoubleDotC/resume-tailor.git
cd resume-tailor

python3 -m venv venv
source venv/bin/activate
pip install mlx-lm fastapi 'uvicorn[standard]' pydantic jinja2 playwright beautifulsoup4
playwright install chromium

# Copy the templates and fill them in
cp master_resume.example.md master_resume.md
cp scraper_config.example.json scraper_config.json
```

Then edit:

1. **`master_resume.md`** — your real resume (name, contact, experience, skills).
   The richer this is, the better the tailoring. The relevance filter uses your
   resume's vocabulary as the baseline.
2. **`scraper_config.json`** — set `linkedin_user_data_dir` to your Chrome
   profile path. macOS default:
   `/Users/<you>/Library/Application Support/Google/Chrome/Default`.
   Adjust locations, categories, and exclusion lists to your search.

## Workflow

### Option A — overnight batch (hands-off)

```bash
./batch_run.sh              # scrape LinkedIn + process all queued jobs
./batch_run.sh --no-scrape  # process existing queue only
./batch_run.sh --dry-run    # preview scraper output, no processing
./batch_run.sh --no-pdf     # skip PDF generation (faster)
```

When done, review with the dashboard:

```bash
./start.sh                  # opens http://localhost:8000
```

Filter by **Done**, expand a row, hit the green **Apply** button — it opens the
job URL and marks it applied.

### Option B — step by step

```bash
source venv/bin/activate

# 1. Scrape (preview first!)
python3 scraper.py --dry-run
python3 scraper.py
python3 scraper.py --config scraper_config_test.json   # tiny test config

# 2. Process the queue
python3 process_queue.py
python3 process_queue.py --no-pdf
python3 process_queue.py --dry-run

# 3. Review
./start.sh
```

**LinkedIn first run:** opens a visible browser window so you can log in once.
Subsequent runs are headless and reuse the session.

### Option C — tailor to a single JD (no scraping, no dashboard)

If you just want to adapt your resume to one specific job posting, skip the
scraper and dashboard entirely and run `mlx_resume_v4.py` directly. This is the
fastest way to use the tool.

**Setup (once):**

```bash
source venv/bin/activate
cp master_resume.example.md master_resume.md   # then edit with your real info
```

**Per job:**

```bash
# 1. Paste the job posting text into job_description.txt (overwrite freely)
#    — or keep multiple JDs as separate files: jd_acme.txt, jd_globex.txt, etc.

# 2. Run the pipeline. The first arg is the output name (no extension).
python3 mlx_resume_v4.py acme_data_analyst \
    --master master_resume.md \
    --job job_description.txt
```

**Outputs land in your current directory:**

- `acme_data_analyst.md` — tailored resume, Markdown
- `acme_data_analyst.pdf` — same, rendered via `pandoc` + `xelatex`
- `acme_data_analyst_debug/` — every pass's raw LLM output (analyzer JSON,
  validator scores, etc.) — handy if a pass misbehaves

First run downloads the MLX model (~5 GB) into your Hugging Face cache. After
that, expect ~3–5 minutes per job on an M-series MacBook Air with 16 GB RAM.

**Useful flags:**

| Flag | What it does |
| --- | --- |
| `--no-pdf` | Markdown only — skips pandoc/xelatex. Fastest. |
| `--no-validate` | Skip passes 3 & 4 (validator + corrector). Trades quality for ~40% speed. |
| `--no-inference` | Skip pass 1b. Blocked keywords stay blocked instead of being semantically inferred. |
| `--no-debug` | Don't write the `_debug/` folder. |
| `--min-score N` | Warn threshold for final ATS score (default `70`). Set higher to be stricter. |
| `--writer-temp F` | Writer-pass temperature, 0.0–1.0 (default `0.7`). Lower = more conservative bullets. |
| `--retries N` | Extra JSON-parse retries per pass on failure (default `2`). |
| `--model PATH` | Use a different MLX model. Default is `mlx-community/Qwen3.5-9B-MLX-4bit`. |
| `--analyzer-model PATH` | Use a separate (often smaller) model for the analyzer pass only. |
| `--thinking` | Enable Qwen thinking mode for the corrector — only useful on 32B+ models. |

**Examples:**

```bash
# Quick draft, Markdown only
python3 mlx_resume_v4.py quick_draft --no-pdf --no-validate

# Stricter ATS bar
python3 mlx_resume_v4.py acme_v2 --min-score 85

# Two JDs in parallel terminals (each spawns its own MLX process — make sure
# you have RAM headroom; 16 GB Macs should run them sequentially instead)
python3 mlx_resume_v4.py acme   --job jd_acme.txt
python3 mlx_resume_v4.py globex --job jd_globex.txt
```

**Tip:** the quality of the output is bounded by your `master_resume.md`. The
writer pass can rephrase, reorder, and emphasize what's already there, but it
won't fabricate experience. If the JD asks for a tool you've genuinely used,
make sure that tool appears somewhere in your master resume — otherwise the
inference pass blocks it as missing.

## How it works

The pipeline runs five passes per job:

1. **Analyzer** — extracts ATS keywords, required/preferred skills, responsibilities, company signals
2. **Inference** — semantic check on blocked keywords (inferable vs. truly missing)
3. **Writer** — rewrites the resume with a 3-tier keyword system
4. **Validator** — scores ATS coverage + bullet quality, flags fabrication
5. **Corrector** — sparse patch for weak bullets / missing keywords (skipped if score ≥ threshold)

Each job runs in a fresh worker subprocess so MLX/Metal memory is fully released
between runs. Default cap is 200 jobs per scrape with a 15-min timeout per job.

## Layout

```
resume-tailor/
├── app.py                       # FastAPI dashboard + API
├── process_queue.py             # Standalone queue processor (no server needed)
├── scraper.py                   # LinkedIn job scraper
├── mlx_resume_v4.py             # 5-pass MLX pipeline (core engine)
├── batch_run.sh                 # Overnight batch: scrape + process
├── start.sh                     # Launch dashboard + open browser
├── master_resume.example.md     # Template — copy to master_resume.md
├── scraper_config.example.json  # Template — copy to scraper_config.json
├── resume_template.tex          # LaTeX template for PDF output
├── static/index.html            # Dashboard (Alpine.js + Tailwind)
├── skills/                      # Reference blueprints (read-only)
└── data/                        # Auto-created at runtime: SQLite DB, logs, outputs
```

## Notes

- This is a personal tool, not a polished product. Expect to read code if
  something breaks.
- LinkedIn rate-limits aggressive scraping. The default delays (2–5 s between
  requests, 5 jobs per query) are conservative on purpose.
- The model and prompts are tuned for analyst / engineer / cybersecurity roles.
  Wildly different fields may need prompt tweaks in `mlx_resume_v4.py`.
- All data stays on your machine. No external API calls.
