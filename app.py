"""
app.py — Resume Tailor v1.0 Web Application
============================================
FastAPI backend serving:
  - Single-page UI (static/index.html)
  - Pipeline trigger (POST /api/applications)
  - SSE progress stream (GET /api/applications/{id}/stream)
  - Application tracker CRUD (GET/PATCH/DELETE /api/applications/*)
  - File downloads (resume .md and .pdf)

Run:
    source venv/bin/activate
    uvicorn app:app --reload --port 8000
"""

from __future__ import annotations

import atexit
import json
import logging
import multiprocessing as mp
import queue
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_MP_CTX = mp.get_context("spawn")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
APPS_DIR  = DATA_DIR / "applications"
DB_PATH   = DATA_DIR / "applications.db"
STATIC_DIR = BASE_DIR / "static"
MASTER_RESUME = BASE_DIR / "master_resume.md"

DATA_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _extract_author_slug(text: str) -> str:
    """
    Read the author name from YAML frontmatter (--- block) and return a
    file-safe slug.  Falls back to 'Resume' if the field is missing.

    Example:  author: "Oussama Taharboucht"  →  "Oussama_Taharboucht"
    """
    m = re.match(r"^---\s*\n(.*?\n)---", text, re.DOTALL)
    if m:
        fm = m.group(1)
        am = re.search(r'^author:\s*["\']?([^"\'\n]+)["\']?', fm, re.MULTILINE)
        if am:
            return re.sub(r"[^\w]+", "_", am.group(1).strip()).strip("_")
    return "Resume"
APPS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _init_db() -> None:
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id                TEXT PRIMARY KEY,
            company           TEXT DEFAULT '',
            role              TEXT DEFAULT '',
            domain            TEXT DEFAULT '',
            jd_text           TEXT NOT NULL,
            status            TEXT DEFAULT 'generating',
            created_at        TEXT,
            updated_at        TEXT,
            final_score       INTEGER,
            ats_score         INTEGER,
            quality_score     INTEGER,
            gap_score         REAL,
            resume_path       TEXT,
            pdf_path          TEXT,
            notes             TEXT DEFAULT '',
            follow_up_date    TEXT,
            fabrication_clean INTEGER DEFAULT 1
        )
    """)
    # Migration: add gap_score to existing DBs that predate this column
    try:
        conn.execute("ALTER TABLE applications ADD COLUMN gap_score REAL")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _db_insert(app_id: str, jd_text: str) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO applications (id, jd_text, status, created_at, updated_at) VALUES (?,?,?,?,?)",
        (app_id, jd_text, "generating", _now_iso(), _now_iso()),
    )
    conn.commit()
    conn.close()

def _db_update(app_id: str, **kwargs) -> None:
    kwargs["updated_at"] = _now_iso()
    cols = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [app_id]
    conn = _get_conn()
    conn.execute(f"UPDATE applications SET {cols} WHERE id=?", vals)
    conn.commit()
    conn.close()

def _db_get(app_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def _db_list() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM applications ORDER BY created_at DESC LIMIT 500"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def _db_delete(app_id: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM applications WHERE id=?", (app_id,))
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Pipeline subprocess worker
# Runs in a fresh OS process so all MLX / Metal memory is freed on exit.
# ---------------------------------------------------------------------------

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
    Entry point for the worker process (spawn context).
    All MLX model weights and Metal allocations are freed when this process exits.
    Communicates only serialisable dicts via mp_q.
    """
    import logging
    import re
    import sys
    from contextlib import redirect_stdout
    from pathlib import Path

    apps_p = Path(apps_dir)
    base_p = Path(base_dir)

    # ── Set up log capture BEFORE importing mlx_resume_v4 so that its
    #    module-level basicConfig() call is a no-op (root already has a handler).
    class _MpLogHandler(logging.Handler):
        def emit(self, r: logging.LogRecord) -> None:
            mp_q.put({"type": "log", "text": self.format(r)})

    hdlr = _MpLogHandler()
    hdlr.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.addHandler(hdlr)
    root.setLevel(logging.INFO)

    sys.path.insert(0, base_dir)
    import mlx_resume_v4 as pm  # basicConfig is now a no-op

    # ── Stdout writer — parses pipeline print() output into SSE events
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
                    pass  # skip path echo lines
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
            "company": jda.company_name if jda else "",
            "role": f"{jda.role_type} / {jda.seniority}" if jda else "",
            "domain": jda.domain if jda else "",
            "final_score": None,
            "ats_score": None,
            "quality_score": None,
            "fabrication_clean": 1,
            "resume_path": None,
            "pdf_path": None,
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
            result["final_score"]       = val.overall_score
            result["ats_score"]         = val.ats_score
            result["quality_score"]     = val.quality_score
            result["gap_score"]         = val.gap_score
            result["fabrication_clean"] = 0 if val.fabrication_flags else 1
            if jda:
                (apps_p / app_id / "validation_report.md").write_text(
                    pm.render_validation_report(val, jda), encoding="utf-8"
                )

        mp_q.put({"type": "result", "data": result})

    except Exception as exc:
        mp_q.put({"type": "error", "message": str(exc)})


# ---------------------------------------------------------------------------
# Pipeline monitoring thread (stays in uvicorn process, bridges mp_q → SSE q)
# ---------------------------------------------------------------------------

WORKER_TIMEOUT_SECS = 900  # 15 minutes max per job; prevents overnight queue stall

# True only in the main uvicorn process. Spawn workers re-import this module
# to resolve function references — this flag prevents startup side-effects
# (DB init, signal handlers, stuck-job reset) from firing in worker processes.
_IS_MAIN_PROCESS = mp.current_process().name == "MainProcess"

executor          = ThreadPoolExecutor(max_workers=1)
pipeline_lock     = threading.Lock()   # must be a plain Lock — acquired in one thread, released in another (executor); RLock tracks owner thread and raises RuntimeError on cross-thread release
active_runs: dict[str, queue.Queue] = {}

# Tracks the currently active worker Process so the shutdown handler can kill it.
_active_proc:      Optional[mp.Process] = None
_active_proc_lock: threading.Lock       = threading.Lock()


def _kill_active_worker() -> None:
    """Terminate the active worker subprocess if one is running. Safe to call from any context."""
    with _active_proc_lock:
        proc = _active_proc
    if proc is None or not proc.is_alive():
        return
    logging.warning("Shutdown: terminating worker process pid=%s", proc.pid)
    proc.terminate()
    proc.join(timeout=5)
    if proc.is_alive():
        logging.warning("Worker did not exit after SIGTERM — sending SIGKILL")
        proc.kill()
        proc.join(timeout=3)


def _sigterm_handler(signum, frame) -> None:
    _kill_active_worker()
    # Re-raise default SIGTERM so uvicorn's own shutdown continues normally
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.raise_signal(signal.SIGTERM)


def _wal_checkpoint() -> None:
    """Flush the WAL file on clean shutdown to prevent -wal/-shm accumulation."""
    try:
        conn = _get_conn()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        logging.info("WAL checkpoint completed")
    except Exception as e:
        logging.warning("WAL checkpoint failed: %s", e)


if _IS_MAIN_PROCESS:
    signal.signal(signal.SIGTERM, _sigterm_handler)
    # atexit runs LIFO: register checkpoint first so worker is killed before checkpoint runs
    atexit.register(_wal_checkpoint)
    atexit.register(_kill_active_worker)


def _run_pipeline_thread(
    app_id: str,
    jd_text: str,
    master_text: str,
    output_name: str,
    no_pdf: bool,
) -> None:
    """
    Spawns the worker process and forwards its events to the SSE queue.
    When the worker exits the OS reclaims all its memory automatically.
    """
    global _active_proc

    sse_q = active_runs[app_id]
    mp_q: mp.Queue = _MP_CTX.Queue()

    proc = _MP_CTX.Process(
        target=_pipeline_worker_process,
        args=(app_id, jd_text, master_text, output_name, no_pdf,
              str(APPS_DIR), str(BASE_DIR), mp_q),
        daemon=True,
    )
    proc.start()
    with _active_proc_lock:
        _active_proc = proc

    deadline = time.monotonic() + WORKER_TIMEOUT_SECS

    try:
        while True:
            # Hard timeout — prevent overnight stall from a stuck model
            if time.monotonic() > deadline:
                logging.error("Job %s timed out after %d minutes — terminating worker",
                              app_id[:8], WORKER_TIMEOUT_SECS // 60)
                proc.terminate()
                _db_update(app_id, status="error")
                sse_q.put({"type": "error",
                           "message": f"Job timed out after {WORKER_TIMEOUT_SECS // 60} minutes"})
                break

            try:
                event = mp_q.get(timeout=1)
            except queue.Empty:
                # 1-second poll — check for unexpected worker death
                if not proc.is_alive() and mp_q.empty():
                    _db_update(app_id, status="error")
                    sse_q.put({"type": "error",
                               "message": f"Worker process exited unexpectedly (code {proc.exitcode})"})
                    break
                continue

            if event["type"] == "result":
                data = event["data"]
                db_fields = {k: v for k, v in data.items() if v is not None}
                db_fields["status"] = "done"
                _db_update(app_id, **db_fields)
                if data.get("final_score") is not None:
                    sse_q.put({"type": "score",
                               "overall": data["final_score"],
                               "ats": data["ats_score"],
                               "quality": data["quality_score"],
                               "gap": data.get("gap_score")})
                sse_q.put({"type": "done", "app_id": app_id,
                           "pdf": bool(data.get("pdf_path"))})
                break

            elif event["type"] == "error":
                _db_update(app_id, status="error")
                sse_q.put(event)
                break

            else:
                sse_q.put(event)

    finally:
        # Always clean up the worker process — whether success, error, timeout, or exception
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=10)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)
        else:
            proc.join(timeout=5)  # reap the zombie even if already dead

        # Close the mp.Queue to release the resource-tracker pipe
        try:
            mp_q.close()
            mp_q.join_thread()
        except Exception:
            pass

        with _active_proc_lock:
            _active_proc = None

        try:
            pipeline_lock.release()
        except RuntimeError as e:
            logging.error("pipeline_lock.release() failed — lock state may be corrupt: %s", e)
        time.sleep(5)  # let SSE clients drain before removing the queue
        active_runs.pop(app_id, None)
        _start_next_queued()  # auto-drain: pick up next queued job if any


def _start_next_queued() -> dict:
    """
    Acquire the pipeline lock and start the oldest queued job.
    Returns a status dict. No-op (returns immediately) if lock is already held.
    """
    if not pipeline_lock.acquire(blocking=False):
        return {"started": False, "reason": "already_running"}

    conn = _get_conn()
    row = conn.execute(
        "SELECT id, jd_text FROM applications WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    conn.close()

    if not row:
        pipeline_lock.release()
        return {"started": False, "reason": "empty_queue"}

    app_id  = row["id"]
    jd_text = row["jd_text"]

    if not MASTER_RESUME.exists():
        pipeline_lock.release()
        logging.error("Master resume not found — cannot process queued job %s", app_id)
        return {"started": False, "reason": "no_master_resume"}

    try:
        _db_update(app_id, status="generating")
        (APPS_DIR / app_id).mkdir(parents=True, exist_ok=True)
        master_text = MASTER_RESUME.read_text(encoding="utf-8")
        q: queue.Queue = queue.Queue()
        active_runs[app_id] = q
        # Lock is already held — executor will release it in the finally block
        executor.submit(_run_pipeline_thread, app_id, jd_text, master_text, f"resume_{app_id[:8]}", False)
        return {"started": True, "active_id": app_id}
    except Exception:
        pipeline_lock.release()
        raise


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

app = FastAPI(title="Resume Tailor")

if _IS_MAIN_PROCESS:
    _init_db()


def _migrate_db() -> None:
    """Idempotent schema migrations — safe to run on every startup."""
    conn = _get_conn()
    for ddl in [
        "ALTER TABLE applications ADD COLUMN job_url   TEXT",
        "ALTER TABLE applications ADD COLUMN source    TEXT",
        "ALTER TABLE applications ADD COLUMN category  TEXT DEFAULT ''",
        "ALTER TABLE applications ADD COLUMN gap_score INTEGER",
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()

if _IS_MAIN_PROCESS:
    _migrate_db()


def _reset_stuck_jobs() -> None:
    """
    On startup, any row still in 'generating' means the server crashed mid-run.
    Reset them to 'queued' so the auto-drain picks them up again.
    """
    conn = _get_conn()
    n = conn.execute(
        "UPDATE applications SET status='queued', updated_at=? WHERE status='generating'",
        (_now_iso(),),
    ).rowcount
    conn.commit()
    conn.close()
    if n:
        logging.warning("Startup: reset %d stuck 'generating' job(s) back to 'queued'", n)


if _IS_MAIN_PROCESS:
    _reset_stuck_jobs()

# ---------------------------------------------------------------------------
# API Models
# ---------------------------------------------------------------------------

class NewApplicationRequest(BaseModel):
    jd_text: str
    job_url: Optional[str] = None
    output_name: Optional[str] = None
    no_pdf: bool = False

class PatchApplicationRequest(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    follow_up_date: Optional[str] = None

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/applications", status_code=201)
def create_application(req: NewApplicationRequest):
    if not req.jd_text.strip():
        raise HTTPException(status_code=400, detail="jd_text is required")

    if not pipeline_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A pipeline run is already in progress. Please wait.")

    if not MASTER_RESUME.exists():
        pipeline_lock.release()
        raise HTTPException(status_code=500, detail=f"Master resume not found: {MASTER_RESUME}")

    app_id = str(uuid.uuid4())
    output_name = req.output_name or f"resume_{app_id[:8]}"

    try:
        (APPS_DIR / app_id).mkdir(parents=True, exist_ok=True)
        _db_insert(app_id, req.jd_text)
        if req.job_url:
            _db_update(app_id, job_url=req.job_url.strip())
        master_text = MASTER_RESUME.read_text(encoding="utf-8")
        q: queue.Queue = queue.Queue()
        active_runs[app_id] = q
        executor.submit(
            _run_pipeline_thread,
            app_id, req.jd_text, master_text, output_name, req.no_pdf
        )
    except Exception:
        pipeline_lock.release()
        raise

    return {"id": app_id}


@app.get("/api/applications/{app_id}/stream")
def stream_application(app_id: str):
    row = _db_get(app_id)
    if not row:
        raise HTTPException(status_code=404, detail="Application not found")

    def event_generator():
        # If run is still active, drain the live queue
        if app_id in active_runs:
            live_q = active_runs[app_id]
            try:
                while True:
                    try:
                        event = live_q.get(timeout=10)
                        yield f"data: {json.dumps(event)}\n\n"
                        if event["type"] in ("done", "error"):
                            break
                    except queue.Empty:
                        # Send keepalive comment
                        yield ": keepalive\n\n"
            except GeneratorExit:
                # Client disconnected — drain queue silently so the worker isn't blocked
                try:
                    while not live_q.empty():
                        live_q.get_nowait()
                except Exception:
                    pass
        else:
            # Run already finished — send synthetic done event from DB
            current = _db_get(app_id)
            if current and current.get("final_score") is not None:
                yield f"data: {json.dumps({'type': 'score', 'overall': current['final_score'], 'ats': current['ats_score'], 'quality': current['quality_score'], 'gap': current.get('gap_score')})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'app_id': app_id, 'pdf': bool(current and current.get('pdf_path'))})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/applications")
def list_applications():
    return _db_list()


@app.get("/api/applications/{app_id}")
def get_application(app_id: str):
    row = _db_get(app_id)
    if not row:
        raise HTTPException(status_code=404, detail="Application not found")
    return row


@app.patch("/api/applications/{app_id}")
def patch_application(app_id: str, req: PatchApplicationRequest):
    row = _db_get(app_id)
    if not row:
        raise HTTPException(status_code=404, detail="Application not found")

    # Valid manual transitions — prevents nonsensical state changes from the UI.
    # "generating" is excluded: the pipeline manages that state internally.
    _VALID_TRANSITIONS: dict[str, set[str]] = {
        "queued":      {"done", "error", "applied"},
        "generating":  set(),  # pipeline-managed; no manual override
        "done":        {"applied", "interviewing", "rejected", "offer", "queued"},
        "error":       {"queued"},
        "applied":     {"interviewing", "rejected", "offer", "done"},
        "interviewing":{"offer", "rejected", "applied"},
        "rejected":    {"queued"},
        "offer":       {"applied", "interviewing"},
    }

    updates: dict = {}
    if req.status is not None:
        all_statuses = set(_VALID_TRANSITIONS.keys())
        if req.status not in all_statuses:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {all_statuses}")
        current_status = row.get("status", "")
        allowed = _VALID_TRANSITIONS.get(current_status, set())
        if req.status != current_status and req.status not in allowed:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot transition from '{current_status}' to '{req.status}'",
            )
        updates["status"] = req.status
    if req.notes is not None:
        updates["notes"] = req.notes
    if req.follow_up_date is not None:
        updates["follow_up_date"] = req.follow_up_date or None

    if updates:
        _db_update(app_id, **updates)

    return _db_get(app_id)


@app.delete("/api/applications/{app_id}", status_code=204)
def delete_application(app_id: str):
    row = _db_get(app_id)
    if not row:
        raise HTTPException(status_code=404, detail="Application not found")
    if row["status"] == "generating":
        raise HTTPException(status_code=409, detail="Cannot delete an active run")

    # Remove files first — if this fails, the DB row is preserved (no orphan record)
    app_folder = APPS_DIR / app_id
    if app_folder.exists():
        try:
            shutil.rmtree(app_folder)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete files: {e}")

    _db_delete(app_id)


@app.get("/api/categories")
def list_categories():
    """Returns distinct non-empty categories present in the DB for dynamic dashboard tabs."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT category FROM applications WHERE category IS NOT NULL AND category != '' ORDER BY category"
    ).fetchall()
    conn.close()
    return [r["category"] for r in rows]


def _safe_resolve(stored_path: str) -> Path:
    """
    Resolve a DB-stored relative path and verify it lives inside APPS_DIR.
    Raises HTTPException(403) on path traversal attempts.
    """
    resolved = (BASE_DIR / stored_path).resolve()
    if not resolved.is_relative_to(APPS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Access denied")
    return resolved


@app.get("/api/applications/{app_id}/resume")
def download_resume(app_id: str):
    row = _db_get(app_id)
    if not row or not row.get("resume_path"):
        raise HTTPException(status_code=404, detail="Resume not available")
    path = _safe_resolve(row["resume_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Resume file not found on disk")
    author_slug  = _extract_author_slug(MASTER_RESUME.read_text(encoding="utf-8"))
    company_slug = re.sub(r"[^\w]+", "_", row.get("company") or "").strip("_")
    filename = f"{author_slug}_Resume_{company_slug}.md" if company_slug else f"{author_slug}_Resume.md"
    return FileResponse(str(path), media_type="text/markdown", filename=filename)


@app.get("/api/applications/{app_id}/pdf")
def download_pdf(app_id: str):
    row = _db_get(app_id)
    if not row or not row.get("pdf_path"):
        raise HTTPException(status_code=404, detail="PDF not available")
    path = _safe_resolve(row["pdf_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF file not found on disk")
    author_slug  = _extract_author_slug(MASTER_RESUME.read_text(encoding="utf-8"))
    company_slug = re.sub(r"[^\w]+", "_", row.get("company") or "").strip("_")
    filename = f"{author_slug}_Resume_{company_slug}.pdf" if company_slug else f"{author_slug}_Resume.pdf"
    return FileResponse(str(path), media_type="application/pdf", filename=filename)


@app.get("/api/queue/status")
def get_queue_status():
    conn = _get_conn()
    queued = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status='queued'"
    ).fetchone()[0]
    active = conn.execute(
        "SELECT id, company FROM applications WHERE status='generating' LIMIT 1"
    ).fetchone()
    conn.close()
    return {
        "queued":         queued,
        "active_id":      active["id"]      if active else None,
        "active_company": active["company"]  if active else None,
    }


@app.post("/api/queue/start", status_code=202)
def start_queue():
    """Manually kick off queue processing. No-op if pipeline is already running."""
    result = _start_next_queued()
    conn = _get_conn()
    queued = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status='queued'"
    ).fetchone()[0]
    conn.close()
    return {**result, "queued": queued}


# Serve the single-page UI — must be LAST so API routes take precedence
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
