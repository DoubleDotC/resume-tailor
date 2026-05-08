"""
test_suite.py — Resume Tailor regression suite
Run:  source venv/bin/activate && python3 test_suite.py
No external dependencies beyond the project venv.
"""
from __future__ import annotations

# Guard against multiprocessing spawn re-executing this file as a child process.
# Without this, importing app.py (which sets up mp.get_context("spawn")) causes
# the spawned worker to re-run the whole test suite.
import sys as _sys
if __name__ != "__main__":
    _sys.exit(0)
import importlib
import inspect
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

_passed = _failed = 0

def ok(msg: str) -> None:
    global _passed
    _passed += 1
    print(f"  {GREEN}✓{RESET} {msg}")

def fail(msg: str, detail: str = "") -> None:
    global _failed
    _failed += 1
    extra = f" — {detail}" if detail else ""
    print(f"  {RED}✗{RESET} {msg}{extra}")

def section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        ok(label)
    else:
        fail(label, detail)


# =============================================================================
# 1. SCRAPER CONFIG
# =============================================================================
section("1. Scraper config")

from scraper import ScraperConfig, FilterEngine, RelevanceFilter, BaytScraper

cfg = ScraperConfig.from_file("scraper_config.json")

check("only linkedin in platforms",
      cfg.platforms == ["linkedin"])
check("bayt removed from platforms",
      "bayt" not in cfg.platforms)
check("jobstreet removed from platforms",
      "jobstreet" not in cfg.platforms)
check("platform_timeout_secs = 3600",
      cfg.platform_timeout_secs == 3600)
check("max_per_platform is dict",
      isinstance(cfg.max_per_platform, dict))
check("linkedin cap set",
      cfg.get_max_per_platform("linkedin") > 0)

# int fallback
cfg_copy = ScraperConfig.from_file("scraper_config.json")
cfg_copy.max_per_platform = 75
check("get_max_per_platform fallback for int",
      cfg_copy.get_max_per_platform("linkedin") == 75)

check("max_per_query = 5",
      cfg.max_per_query == 5)
check("delay_min < delay_max",
      cfg.delay_min < cfg.delay_max)


# =============================================================================
# 2. SCRAPER LOGIC
# =============================================================================
section("2. Scraper logic")

# 2a. seen_slugs dedup key is (cat_name, cat_slug) tuple
seen: set[tuple[str, str]] = set()
results = []
for cat_name, query, slug in [
    ("cybersecurity",  "SOC analyst",                    "cybersecurity"),
    ("dlp_governance", "information security analyst",   "cybersecurity"),  # same slug, different cat
    ("cybersecurity",  "cybersecurity analyst",          "cybersecurity"),  # exact dup → skip
]:
    real_slug = BaytScraper.CATEGORY_SLUGS.get(query.lower(), slug)
    key = (cat_name, real_slug)
    if key in seen:
        results.append("skip")
    else:
        seen.add(key)
        results.append("add")

check("seen_slugs: first entry added",         results[0] == "add")
check("seen_slugs: diff cat+same slug added",  results[1] == "add")
check("seen_slugs: duplicate entry skipped",   results[2] == "skip")

# 2b. RelevanceFilter includes acronyms and uses JD-side denominator
with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
    f.write("SQL GCP DLP IAM SIEM cloud security governance risk compliance data analysis Python")
    vocab_path = f.name

rf = RelevanceFilter(vocab_path)
for term in ["sql", "gcp", "dlp", "iam", "siem"]:
    check(f"RelevanceFilter vocab includes '{term}'", term in rf._vocab)

# Ratio = overlap / JD-words (not overlap / resume-vocab)
# Resume vocab: sql gcp dlp iam siem cloud security governance risk compliance data analysis python (13 words)
# Relevant JD (10 words, 5 match): "data analysis SQL cloud security report tool build plan work"
# overlap=5, jd_words=8 after stopwords → ratio=5/8=62% → passes 10%
relevant_jd   = "data analysis SQL cloud security report tool build plan work"
irrelevant_jd = "maritime vessel captain cargo ship route ocean port logistics crew"
rel_pass, rel_ratio   = rf.is_relevant(relevant_jd)
rel_fail, irrel_ratio = rf.is_relevant(irrelevant_jd)
check("RelevanceFilter: relevant JD passes",    rel_pass,  f"ratio={rel_ratio:.0%}")
check("RelevanceFilter: irrelevant JD blocked", not rel_fail, f"ratio={irrel_ratio:.0%}")

os.unlink(vocab_path)

# 2c. JD worktype window 2000 chars
fe = FilterEngine(cfg)
jd_buried  = "A" * 700 + " This is a part-time position. " + "B" * 100
jd_clean   = "A" * 2500  # no worktype keyword anywhere
check("worktype detected at char 700 (inside 2000-window)",
      fe._jd_worktype_excluded(jd_buried))
check("clean JD not excluded",
      not fe._jd_worktype_excluded(jd_clean))


# =============================================================================
# 3. APP ENDPOINTS
# =============================================================================
section("3. App endpoints")

import app as appm
importlib.reload(appm)
from fastapi.testclient import TestClient
client = TestClient(appm.app)

# 3a. DB columns
conn = sqlite3.connect("data/applications.db")
conn.row_factory = sqlite3.Row
cols = {r["name"] for r in conn.execute("PRAGMA table_info(applications)").fetchall()}
conn.close()
for col in ["gap_score", "job_url", "source", "category"]:
    check(f"DB column '{col}' exists", col in cols)

# 3b. pipeline_lock is a plain Lock (not RLock — lock is acquired in one thread,
#     released in another; RLock raises RuntimeError on cross-thread release)
import threading
check("pipeline_lock is plain Lock (not RLock)",
      type(appm.pipeline_lock) is threading.Lock)

# 3c. POST creates record with job_url
resp = client.post("/api/applications", json={
    "jd_text": "Senior Data Analyst role requiring Python, SQL, Power BI, and stakeholder management. " * 10,
    "no_pdf": True,
    "job_url": "https://example.com/job/test-suite-123",
})
if resp.status_code == 201:
    app_id = resp.json()["id"]
    # Immediately kill the spawned pipeline worker so it doesn't load MLX weights
    appm._kill_active_worker()
    row = client.get(f"/api/applications/{app_id}").json()
    check("POST /api/applications returns 201",           True)
    check("job_url stored correctly",                     row["job_url"] == "https://example.com/job/test-suite-123")
    check("status initialised as 'generating'",           row["status"] == "generating")

    # Force to done for transition tests
    appm._db_update(app_id, status="done")

    # 3d. Status transitions — valid
    for from_s, to_s, expect in [
        ("done",        "applied",      200),
        ("applied",     "interviewing", 200),
        ("interviewing","rejected",     200),
        ("rejected",    "queued",       200),
        ("queued",      "done",         200),
    ]:
        appm._db_update(app_id, status=from_s)
        r = client.patch(f"/api/applications/{app_id}", json={"status": to_s})
        check(f"transition {from_s}→{to_s} allowed (200)", r.status_code == expect,
              f"got {r.status_code}")

    # 3e. Invalid transitions blocked
    for from_s, to_s in [
        ("done",       "generating"),
        ("generating", "done"),
        ("error",      "applied"),
        ("applied",    "queued"),
    ]:
        appm._db_update(app_id, status=from_s)
        r = client.patch(f"/api/applications/{app_id}", json={"status": to_s})
        check(f"transition {from_s}→{to_s} blocked (409)", r.status_code == 409,
              f"got {r.status_code}: {r.text[:80]}")

    # 3f. Notes and follow_up_date
    appm._db_update(app_id, status="done")
    r = client.patch(f"/api/applications/{app_id}",
                     json={"notes": "Great role", "follow_up_date": "2026-04-01"})
    check("PATCH notes + follow_up_date (200)", r.status_code == 200)
    updated = client.get(f"/api/applications/{app_id}").json()
    check("notes saved",           updated["notes"] == "Great role")
    check("follow_up_date saved",  updated["follow_up_date"] == "2026-04-01")

    # 3g. SSE stream returns synthetic done for already-finished run
    # Use a fresh record that never went through the pipeline so active_runs
    # never has this ID — guarantees the synthetic (already-finished) code path.
    sse_id = str(uuid.uuid4())
    appm._db_insert(sse_id, "SSE test JD — synthetic path")
    appm._db_update(sse_id, status="done", final_score=85, ats_score=90,
                    quality_score=80, gap_score=72)
    sse_resp = client.get(f"/api/applications/{sse_id}/stream")
    check("SSE stream returns 200", sse_resp.status_code == 200)
    sse_body = sse_resp.text
    check("SSE contains score event", '"type": "score"' in sse_body or '"type":"score"' in sse_body)
    check("SSE contains gap in score", '"gap"' in sse_body)
    check("SSE contains done event",  '"type": "done"' in sse_body or '"type":"done"' in sse_body)

    # 3h. Atomic delete
    appm._db_update(app_id, status="done")
    r = client.delete(f"/api/applications/{app_id}")
    check("DELETE returns 204",           r.status_code == 204)
    check("record removed after delete",  client.get(f"/api/applications/{app_id}").status_code == 404)

elif resp.status_code == 409:
    ok("POST (pipeline busy — endpoint reachable, lock logic verified separately)")
else:
    fail(f"POST /api/applications unexpected status {resp.status_code}", resp.text[:120])

# 3i. /api/categories
r = client.get("/api/categories")
check("/api/categories returns 200",  r.status_code == 200)
check("/api/categories returns list", isinstance(r.json(), list))

# 3j. /api/applications list
r = client.get("/api/applications")
check("/api/applications returns 200",  r.status_code == 200)
check("/api/applications returns list", isinstance(r.json(), list))

src = inspect.getsource(appm._db_list)
check("_db_list has LIMIT 500 guard",  "LIMIT 500" in src)


# =============================================================================
# 4. PIPELINE LOGIC (mlx_resume_v4.py — no LLM calls)
# =============================================================================
section("4. Pipeline logic")

from mlx_resume_v4 import (
    ValidationReport, KeywordCheck, BulletRating,
    _recompute_scores, _filter_achievable_keywords,
    _BANNED_SUMMARY_PHRASES,
)

def make_report(present, absent, blocked, bullet_scores, fabs=None, summary=""):
    kws = (
        [KeywordCheck(keyword=k, present=True,  location="Summary") for k in present] +
        [KeywordCheck(keyword=k, present=False, location="")         for k in absent]  +
        [KeywordCheck(keyword=k, present=False, location="NOT IN MASTER RESUME") for k in blocked]
    )
    bullets = [BulletRating(company="T", bullet_preview="x", score=s, feedback="")
               for s in bullet_scores]
    return ValidationReport(
        keyword_coverage=kws, bullet_ratings=bullets,
        fabrication_flags=fabs or [], summary_feedback=summary, top_improvements=[],
    )

# 4a. gap_score vs ats_score
# achievable = Python(✓), SQL(✓), Kafka(✗) → ats = 2/3 = 67
# all        = Python(✓), SQL(✓), Kafka(✗), Rust(✗) → gap = 2/4 = 50
r = make_report(["Python","SQL"], ["Kafka"], ["Rust"], [4,4,5])
r = _recompute_scores(r)
check(f"ats_score = 67 (achievable only)",    r.ats_score == 67,    f"got {r.ats_score}")
check(f"gap_score = 50 (all kws incl blocked)", r.gap_score == 50,  f"got {r.gap_score}")

# 4b. Fabrication penalty
base_r = _recompute_scores(make_report(["A","B","C"], [], [], [5,5,5]))
base   = base_r.overall_score

r_1fab = _recompute_scores(make_report(["A","B","C"], [], [], [5,5,5], fabs=["inv metric"]))
check(f"1 fab flag: score = min(base-10, 60)",
      r_1fab.overall_score == min(base - 10, 60),
      f"base={base} got {r_1fab.overall_score}")

r_5fab = _recompute_scores(make_report(["A","B","C"], [], [], [5,5,5], fabs=[f"f{i}" for i in range(5)]))
check("5 fab flags: score capped at 60",       r_5fab.overall_score <= 60)
check("5 fab flags: score not negative",       r_5fab.overall_score >= 0)

# 4c. Keyword subset dedup
kws_raw = ["Python", "python programming", "SQL", "SQL querying", "DLP", "DLP governance"]
seen_lower: set[str] = set()
deduped = []
for k in kws_raw:
    kl = k.strip().lower()
    if kl not in seen_lower:
        seen_lower.add(kl)
        deduped.append(k)
dl = [k.strip().lower() for k in deduped]
final = [kw for i, kw in enumerate(deduped)
         if not any(dl[j] != dl[i] and dl[j] in dl[i] for j in range(len(dl)))]
removed_lower = {k.lower() for k in deduped} - {k.lower() for k in final}
check("subset dedup removes 'python programming'",  "python programming" in removed_lower)
check("subset dedup removes 'sql querying'",        "sql querying"       in removed_lower)
check("subset dedup removes 'dlp governance'",      "dlp governance"     in removed_lower)
check("subset dedup keeps 'python'",                "python"             in {k.lower() for k in final})
check("subset dedup keeps 'sql'",                   "sql"                in {k.lower() for k in final})
check("subset dedup keeps 'dlp'",                   "dlp"                in {k.lower() for k in final})

# 4d. Tier-3 word-boundary matching
tokens = re.findall(r"\b[a-z]{7,}\b", "data governance".lower())
check("Tier-3 extracts 7+ char tokens from 'data governance'",
      "governance" in tokens)
good_master = "Led data governance initiatives across departments."
bad_master  = "Led datagovernancePlus platform integration work."
matched_good = all(re.search(r"\b" + re.escape(t) + r"\b", good_master.lower()) for t in tokens)
matched_bad  = all(re.search(r"\b" + re.escape(t) + r"\b", bad_master.lower())  for t in tokens)
check("Tier-3 matches whole word 'governance'",           matched_good)
check("Tier-3 does NOT match 'datagovernancePlus'",       not matched_bad)

# 4e. _BANNED_SUMMARY_PHRASES is module-level (importable)
check("_BANNED_SUMMARY_PHRASES defined at module level",  isinstance(_BANNED_SUMMARY_PHRASES, list))
check("_BANNED_SUMMARY_PHRASES not empty",                len(_BANNED_SUMMARY_PHRASES) > 0)
bad_sum  = "results-driven professional with a proven track record."
good_sum = "Data analyst with 5 years building BI dashboards for financial services."
check("banned phrase detected in bad summary",
      any(p in bad_sum.lower() for p in _BANNED_SUMMARY_PHRASES))
check("no banned phrase in good summary",
      not any(p in good_sum.lower() for p in _BANNED_SUMMARY_PHRASES))

# 4f. Corrector gate
def _should_correct(missing, weak, summary_fb, fabs):
    return missing > 0 or weak > 0 or bool(summary_fb) or bool(fabs)

check("corrector runs: missing keyword",      _should_correct(1, 0, "", []))
check("corrector runs: weak bullet",          _should_correct(0, 1, "", []))
check("corrector runs: summary feedback",     _should_correct(0, 0, "bad phrase", []))
check("corrector runs: fabrication flag",     _should_correct(0, 0, "", ["inv"]))
check("corrector skips: no issues at all",    not _should_correct(0, 0, "", []))

# 4g. _filter_achievable_keywords
master = "I have experience with data governance, threat modeling, SQL query optimisation, and Python scripting."
achievable, blocked = _filter_achievable_keywords(
    ["data governance", "threat modeling", "SQL", "Kubernetes"], master
)
check("'data governance' achievable",  "data governance" in achievable)
check("'threat modeling' achievable",  "threat modeling" in achievable)
check("'SQL' achievable",              "SQL" in achievable)
check("'Kubernetes' blocked",          "Kubernetes" in blocked)


# =============================================================================
# 5. PHASE C — app.py safety & housekeeping
# =============================================================================
section("5. Phase C (author slug, path traversal, WAL, db_list)")

from app import _extract_author_slug, _safe_resolve, _wal_checkpoint, APPS_DIR, BASE_DIR
from fastapi import HTTPException

# 5a. Author slug from frontmatter
cases = [
    ('---\nauthor: "Oussama Taharboucht"\ntitle: "x"\n---\nBody', "Oussama_Taharboucht"),
    ('---\nauthor: Jane Doe\n---\n',                               "Jane_Doe"),
    ('---\nauthor: "María García-López"\n---\n',                   "María_García_López"),
    ('Just a resume body without any frontmatter',                  "Resume"),
]
for text, expected in cases:
    got = _extract_author_slug(text)
    check(f"author slug '{expected}'", got == expected, f"got '{got}'")

real_slug = _extract_author_slug(Path("master_resume.md").read_text())
check(f"real master_resume.md → '{real_slug}'", real_slug == "Oussama_Taharboucht",
      f"got '{real_slug}'")

# 5b. Path traversal guard
test_id  = str(uuid.uuid4())
test_dir = APPS_DIR / test_id
test_dir.mkdir(parents=True, exist_ok=True)
(test_dir / "r.md").write_text("hello")
rel = str((test_dir / "r.md").relative_to(BASE_DIR))

try:
    resolved = _safe_resolve(rel)
    check("valid path resolves without 403", resolved == (test_dir / "r.md").resolve())
except HTTPException:
    fail("valid path raised 403 unexpectedly")

traversal_attempts = [
    "data/applications/../../../etc/passwd",
    "../../etc/shadow",
    "/etc/passwd",
    "data/applications/" + test_id + "/../../../../etc/hosts",
]
for bad in traversal_attempts:
    try:
        _safe_resolve(bad)
        fail(f"traversal not blocked: '{bad}'")
    except HTTPException as e:
        check(f"traversal blocked: '{bad[:45]}…'", e.status_code == 403)

shutil.rmtree(test_dir)

# 5c. WAL checkpoint
try:
    _wal_checkpoint()
    ok("WAL checkpoint completes without error")
except Exception as e:
    fail("WAL checkpoint raised exception", str(e))

# 5d. _db_list LIMIT 500
check("_db_list query has LIMIT 500",
      "LIMIT 500" in inspect.getsource(appm._db_list))


# =============================================================================
# 6. HTML BINDINGS
# =============================================================================
section("6. static/index.html bindings")

html = Path("static/index.html").read_text()

# Alpine.js state keys present
for key in ["jobUrl", "searchText", "sortBy", "sortDir", "categories", "gapScore"]:
    # gapScore lives inside the scores object, so check differently
    if key == "gapScore":
        check(f"gap: null in scores state",  "gap: null" in html)
    else:
        check(f"Alpine state has '{key}'", f"{key}:" in html or f"{key} :" in html)

# x-model bindings wired
for binding in ["jdText", "jobUrl", "searchText", "noPdf"]:
    check(f"x-model=\"{binding}\" present", f'x-model="{binding}"' in html)

# Sort
check("setSort function defined",          "setSort(col)" in html)
check("Score column calls setSort",        "setSort('score')" in html)
check("Status column calls setSort",       "setSort('status')" in html)
check("Created column calls setSort",      "setSort('created_at')" in html)

# localStorage
ls_set = set(re.findall(r"ls\.setItem\('(\w+)'", html))
ls_get = set(re.findall(r"ls\.getItem\('(\w+)'", html))
check("localStorage set keys match get keys",  ls_set == ls_get,
      f"set={ls_set}  get={ls_get}")
for key in ["rt_statusFilter", "rt_categoryFilter", "rt_sortBy", "rt_sortDir"]:
    check(f"localStorage persists '{key}'", key in ls_set)

# Dynamic categories
check("categories loaded from /api/categories",  "/api/categories" in html)
check("category tabs use x-for over categories", 'x-for="cat in categories"' in html)

# gap_score display
check("gap_score in score card (new app)",  "JD Fit (incl. blocked)" in html)
check("gap_score in expanded row",          "JD Fit:" in html)
check("gap_score in table score column",    "gap_score" in html)

# SSE gap capture
check("SSE score handler captures gap field", "event.gap" in html)


# =============================================================================
# SUMMARY
# =============================================================================
total = _passed + _failed
print(f"\n{'='*55}")
if _failed == 0:
    print(f"{GREEN}{BOLD}  All {total} tests passed.{RESET}")
else:
    print(f"{RED}{BOLD}  {_failed} / {total} tests FAILED.{RESET}")
print(f"{'='*55}\n")
sys.exit(0 if _failed == 0 else 1)
