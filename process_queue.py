#!/usr/bin/env python3
"""
process_queue.py — Standalone queue processor for Resume Tailor
===============================================================
Processes all queued jobs from the DB directly. No web server needed.
Use this for overnight batch runs instead of starting uvicorn.

Usage:
    source venv/bin/activate
    python3 process_queue.py                         # process all queued jobs
    python3 process_queue.py --log data/batch.log    # also write to log file
    python3 process_queue.py --no-pdf                # skip PDF generation
    python3 process_queue.py --dry-run               # show queue count only
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import queue
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DIR             = Path(__file__).parent
APPS_DIR             = BASE_DIR / "data" / "applications"
DB_PATH              = BASE_DIR / "data" / "applications.db"
MASTER_RESUME        = BASE_DIR / "master_resume.md"
WORKER_TIMEOUT_SECS  = 900  # 15 minutes per job

_MP_CTX = mp.get_context("spawn")

# ---------------------------------------------------------------------------
# Terminal colours
# ---------------------------------------------------------------------------

GREEN  = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"
RED    = "\033[0;31m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

# ---------------------------------------------------------------------------
# Logging — verbose to file, warnings-only to stdout
# ---------------------------------------------------------------------------

log = logging.getLogger("process_queue")

def _setup_logging(log_file: Optional[str]) -> None:
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.WARNING)
    root.addHandler(ch)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        root.addHandler(fh)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _ensure_schema() -> None:
    """Add gap_score column if it doesn't exist yet (migration for older DBs)."""
    conn = _get_conn()
    try:
        conn.execute("ALTER TABLE applications ADD COLUMN gap_score REAL")
        conn.commit()
        log.info("Migrated DB: added gap_score column")
    except sqlite3.OperationalError:
        pass  # column already exists
    finally:
        conn.close()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _db_update(app_id: str, **kwargs) -> None:
    kwargs["updated_at"] = _now_iso()
    cols = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [app_id]
    conn = _get_conn()
    conn.execute(f"UPDATE applications SET {cols} WHERE id=?", vals)
    conn.commit()
    conn.close()

def _reset_stuck_jobs() -> int:
    """Reset generating→queued from a previous crash. Returns number reset."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE applications SET status='queued', updated_at=? WHERE status='generating'",
        (_now_iso(),),
    )
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count

def _count_queued() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) FROM applications WHERE status='queued'").fetchone()
    conn.close()
    return row[0]

def _fetch_next_queued() -> Optional[sqlite3.Row]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, jd_text, company, role FROM applications "
        "WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    conn.close()
    return row

# ---------------------------------------------------------------------------
# Worker subprocess — copied verbatim from app.py (self-contained)
# ---------------------------------------------------------------------------

def _extract_author_slug(text: str) -> str:
    m = re.match(r"^---\s*\n(.*?\n)---", text, re.DOTALL)
    if m:
        fm = m.group(1)
        am = re.search(r'^author:\s*["\']?([^"\'\n]+)["\']?', fm, re.MULTILINE)
        if am:
            return re.sub(r"[^\w]+", "_", am.group(1).strip()).strip("_")
    return "Resume"


def _pipeline_worker_process(
    app_id: str,
    jd_text: str,
    master_text: str,
    output_name: str,
    no_pdf: bool,
    apps_dir: str,
    base_dir: str,
    mp_q: "mp.Queue",
) -> None:
    """
    Runs in a fresh OS process (spawn context). All MLX/Metal memory freed on exit.
    Communicates via mp_q with serialisable dicts only.
    """
    import logging
    import re
    import sys
    from contextlib import redirect_stdout
    from pathlib import Path

    apps_p = Path(apps_dir)
    base_p = Path(base_dir)

    class _MpLogHandler(logging.Handler):
        def emit(self, r: logging.LogRecord) -> None:
            mp_q.put({"type": "log", "text": self.format(r)})

    hdlr = _MpLogHandler()
    hdlr.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.addHandler(hdlr)
    root.setLevel(logging.INFO)

    sys.path.insert(0, base_dir)
    import mlx_resume_v4 as pm

    class _StdoutWriter:
        def __init__(self) -> None:
            self._buf = ""

        def write(self, s: str) -> None:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                text = line.rstrip()
                if not text or text.startswith("═"):
                    continue
                stripped = text.strip()
                if stripped.startswith("PASS "):
                    mp_q.put({"type": "section", "text": stripped})
                elif stripped.startswith(("Resume ", "PDF ", "Debug ")):
                    pass
                else:
                    m = re.match(r"\s{2,}([\w &/]+?)\s*:\s*(.+)", text)
                    if m and len(m.group(1)) < 30:
                        mp_q.put({"type": "stat", "key": m.group(1).strip(), "value": m.group(2).strip()})
                    else:
                        mp_q.put({"type": "log", "text": text})

        def flush(self) -> None:
            pass

    debug_dir = apps_p / app_id / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    try:
        with redirect_stdout(_StdoutWriter()):
            resume, jda, val = pm.run_pipeline(
                master_text, jd_text, pm.Config(), debug_dir=debug_dir
            )

        if not resume:
            mp_q.put({"type": "error", "message": "Pipeline failed — no resume generated."})
            return

        result: dict = {
            "company":          jda.company_name if jda else "",
            "role":             f"{jda.role_type} / {jda.seniority}" if jda else "",
            "domain":           jda.domain if jda else "",
            "final_score":      None,
            "ats_score":        None,
            "quality_score":    None,
            "gap_score":        None,
            "fabrication_clean": 1,
            "resume_path":      None,
            "pdf_path":         None,
        }

        company_slug = re.sub(r"[^\w]+", "_", (jda.company_name if jda and jda.company_name else "")).strip("_")
        author_slug  = _extract_author_slug(master_text)
        file_stem = f"{author_slug}_Resume_{company_slug}" if company_slug else f"{author_slug}_Resume"

        md_path = apps_p / app_id / f"{file_stem}.md"
        md_path.write_text(pm.render_pandoc_markdown(resume), encoding="utf-8")
        result["resume_path"] = str(md_path.relative_to(base_p))

        if not no_pdf:
            pdf_path = apps_p / app_id / f"{file_stem}.pdf"
            if pm.convert_to_pdf(md_path, pdf_path, pm.Config()):
                result["pdf_path"] = str(pdf_path.relative_to(base_p))

        if val:
            result["final_score"]        = val.overall_score
            result["ats_score"]          = val.ats_score
            result["quality_score"]      = val.quality_score
            result["gap_score"]          = val.gap_score
            result["fabrication_clean"]  = 0 if val.fabrication_flags else 1
            if jda:
                (apps_p / app_id / "validation_report.md").write_text(
                    pm.render_validation_report(val, jda), encoding="utf-8"
                )

        mp_q.put({"type": "result", "data": result})

    except Exception as exc:
        mp_q.put({"type": "error", "message": str(exc)})


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

_shutdown_requested = False

def _handle_sigint(signum, frame) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    print(f"\n{YELLOW}⚠  Interrupt — finishing current job then stopping.{RESET}")


_PASS_LABELS: dict[str, str] = {
    "PASS 1":  "Analyzing JD",
    "PASS 1B": "Inferring skills",
    "PASS 2":  "Writing resume",
    "PASS 3":  "Validating",
    "PASS 4":  "Correcting",
}


def run_job(
    index: int,
    total: int,
    app_id: str,
    jd_text: str,
    company: str,
    role: str,
    master_text: str,
    no_pdf: bool,
    log_fh,
) -> bool:
    """Spawn worker, stream progress to terminal, update DB. Returns True on success."""
    label  = f"{company}  ·  {role}" if company else app_id[:8]
    prefix = "       "

    header_line = f"\n{BOLD}[{index}/{total}]{RESET}  {label}"
    print(header_line)
    if log_fh:
        log_fh.write(f"\n[{index}/{total}]  {label}\n")
        log_fh.flush()

    _db_update(app_id, status="generating")
    (APPS_DIR / app_id).mkdir(parents=True, exist_ok=True)

    mp_q: mp.Queue = _MP_CTX.Queue()
    proc = _MP_CTX.Process(
        target=_pipeline_worker_process,
        args=(app_id, jd_text, master_text, f"resume_{app_id[:8]}", no_pdf,
              str(APPS_DIR), str(BASE_DIR), mp_q),
        daemon=True,
    )
    proc.start()

    deadline = time.monotonic() + WORKER_TIMEOUT_SECS
    success  = False

    try:
        while True:
            # Hard timeout
            if time.monotonic() > deadline:
                proc.terminate()
                _db_update(app_id, status="error")
                msg = f"Timed out after {WORKER_TIMEOUT_SECS // 60} minutes"
                print(f"{prefix}{RED}✗  {msg}{RESET}")
                if log_fh:
                    log_fh.write(f"{prefix}✗  {msg}\n"); log_fh.flush()
                break

            try:
                event = mp_q.get(timeout=1)
            except queue.Empty:
                # Poll timeout — check for unexpected worker death
                if not proc.is_alive() and mp_q.empty():
                    _db_update(app_id, status="error")
                    msg = f"Worker exited unexpectedly (code {proc.exitcode})"
                    print(f"{prefix}{RED}✗  {msg}{RESET}")
                    if log_fh:
                        log_fh.write(f"{prefix}✗  {msg}\n"); log_fh.flush()
                    break
                continue

            etype = event.get("type")

            if etype == "section":
                raw = event["text"]              # e.g. "PASS 1 · Analyzer"
                key = raw.split("·")[0].strip().upper()
                label_text = _PASS_LABELS.get(key, raw)
                line = f"{prefix}{DIM}›  {label_text}...{RESET}"
                print(line, flush=True)
                if log_fh:
                    log_fh.write(f"{prefix}›  {label_text}...\n"); log_fh.flush()

            elif etype == "result":
                data = event["data"]
                db_fields = {k: v for k, v in data.items() if v is not None}
                db_fields["status"] = "done"
                _db_update(app_id, **db_fields)

                sc   = data.get("final_score")
                ats  = data.get("ats_score")
                qual = data.get("quality_score")
                pdf  = "  →  PDF" if data.get("pdf_path") else ""
                score_str = f"Score {sc}  (ATS {ats} · Quality {qual})" if sc is not None else "Done"
                line = f"{prefix}{GREEN}✓  {score_str}{pdf}{RESET}"
                print(line)
                if log_fh:
                    log_fh.write(f"{prefix}✓  {score_str}{pdf}\n"); log_fh.flush()
                success = True
                break

            elif etype == "error":
                msg = event.get("message", "Unknown error")
                _db_update(app_id, status="error")
                print(f"{prefix}{RED}✗  {msg}{RESET}")
                if log_fh:
                    log_fh.write(f"{prefix}✗  {msg}\n"); log_fh.flush()
                break

            elif etype == "log":
                log.info("worker: %s", event.get("text", ""))
                if log_fh:
                    log_fh.write(f"  {event.get('text','')}\n")

    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=10)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)
        else:
            proc.join(timeout=5)
        try:
            mp_q.close()
            mp_q.join_thread()
        except Exception:
            pass

    return success


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process all queued jobs directly — no web server needed",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--log",     default=None,  help="Also write output to this log file")
    parser.add_argument("--no-pdf",  action="store_true", help="Skip PDF generation")
    parser.add_argument("--dry-run", action="store_true", help="Show queue count without processing")
    args = parser.parse_args()

    _setup_logging(args.log)
    signal.signal(signal.SIGINT, _handle_sigint)

    if not DB_PATH.exists():
        print(f"{RED}DB not found at {DB_PATH} — run ./start.sh once to create it.{RESET}")
        sys.exit(1)

    if not MASTER_RESUME.exists():
        print(f"{RED}master_resume.md not found.{RESET}")
        sys.exit(1)

    _ensure_schema()

    stuck = _reset_stuck_jobs()
    if stuck:
        print(f"{YELLOW}⚠  Reset {stuck} stuck 'generating' job(s) back to 'queued'.{RESET}")

    total = _count_queued()
    if total == 0:
        print("Queue is empty — nothing to process.")
        sys.exit(0)

    est_mins = total * 5
    print(f"\n{BOLD}Queue: {total} job(s)  (~{est_mins} min total){RESET}")

    if args.dry_run:
        sys.exit(0)

    log_fh = None
    if args.log:
        log_fh = open(args.log, "a", encoding="utf-8")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_fh.write(f"\n{'='*60}\nQueue run started: {now}  ({total} jobs)\n{'='*60}\n")
        log_fh.flush()

    master_text = MASTER_RESUME.read_text(encoding="utf-8")

    done_count  = 0
    error_count = 0
    scores: list[int] = []
    index       = 0
    start_time  = time.monotonic()

    try:
        while not _shutdown_requested:
            row = _fetch_next_queued()
            if not row:
                break

            index += 1
            ok = run_job(
                index=index,
                total=total,
                app_id=row["id"],
                jd_text=row["jd_text"],
                company=row["company"] or "",
                role=row["role"] or "",
                master_text=master_text,
                no_pdf=args.no_pdf,
                log_fh=log_fh,
            )

            if ok:
                done_count += 1
                conn = _get_conn()
                r = conn.execute(
                    "SELECT final_score FROM applications WHERE id=?", (row["id"],)
                ).fetchone()
                conn.close()
                if r and r[0] is not None:
                    scores.append(r[0])
            else:
                error_count += 1

    finally:
        elapsed    = int(time.monotonic() - start_time)
        mins, secs = divmod(elapsed, 60)
        avg        = int(sum(scores) / len(scores)) if scores else 0

        lines = [
            f"\n{BOLD}{'━'*48}",
            f"  Session Summary",
            f"{'━'*48}{RESET}",
            f"  Processed : {index} job(s)",
            f"  Done      : {GREEN}{done_count}{RESET}" + (f"  (avg score {avg})" if avg else ""),
            f"  Errors    : {(RED if error_count else '')}{error_count}{RESET}",
            f"  Duration  : {mins}m {secs}s",
            f"{BOLD}{'━'*48}{RESET}",
        ]
        summary = "\n".join(lines) + "\n"
        print(summary)

        if log_fh:
            plain = re.sub(r"\033\[[0-9;]*m", "", summary)
            log_fh.write(plain)
            log_fh.close()

        if _shutdown_requested:
            print(f"{YELLOW}Stopped early — remaining queued jobs are untouched.{RESET}")


if __name__ == "__main__":
    main()
