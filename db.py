"""
db.py - saving and loading everything (submissions, documents, runs, overrides).

Where the data lives:
  - If DATABASE_URL is set (in secrets), we use that hosted Postgres database. Data survives
    restarts and is shared by everyone who opens the link. This is what the live app uses.
  - Otherwise we use a local SQLite file (vendor_onboarding.db) - handy on your own laptop.

Same code for both, thanks to SQLAlchemy. If you know SQL: these are just 5 tables.

  submissions      one row per version of a vendor's submission (v1, v2 share the same request_ref)
  documents        the uploaded files themselves (stored in the database, so nothing is lost)
  runs             one row each time the checks run: status, summary, every check result, the email
  overrides        a human changing a decision, with who / when / why  (the audit trail)
  extraction_cache what the AI read off each file, so the same file is never sent to the AI twice
"""

import json
import os
import threading
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer, LargeBinary, MetaData, String,
                        Table, Text, create_engine, func, insert, select, text, update)

metadata = MetaData()

submissions = Table(
    "submissions", metadata,
    Column("id", Integer, primary_key=True),
    Column("request_ref", String(20), index=True),     # e.g. REQ-0007, shared by all versions
    Column("version", Integer, default=1),
    Column("parent_id", Integer, ForeignKey("submissions.id"), nullable=True),
    Column("vendor_name", String(200)),
    Column("source", String(20)),                      # "test_case" or "form"
    Column("case_id", String(60), nullable=True),      # which test case it came from, if any
    Column("payload", Text),                           # the full form as JSON
    Column("created_at", DateTime(timezone=True)),
)

documents = Table(
    "documents", metadata,
    Column("id", Integer, primary_key=True),
    Column("submission_id", Integer, ForeignKey("submissions.id"), index=True),
    Column("slot", String(40)),
    Column("filename", String(200)),
    Column("mime_type", String(60)),
    Column("sha256", String(64)),
    Column("content", LargeBinary),
)

runs = Table(
    "runs", metadata,
    Column("id", Integer, primary_key=True),
    Column("submission_id", Integer, ForeignKey("submissions.id"), index=True),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("seconds", Float),
    Column("status", String(40)),
    Column("summary", Text),
    Column("reason_codes", Text),     # JSON list
    Column("checks", Text),           # JSON list of every check result
    Column("extractions", Text),      # JSON: what the AI read off each document
    Column("email", Text),            # JSON: {to, subject, body} or null
    Column("vendor_id", String(20), nullable=True),
)

overrides = Table(
    "overrides", metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", Integer, ForeignKey("runs.id"), index=True),
    Column("new_status", String(40)),
    Column("reason", Text),
    Column("reviewer", String(100)),
    Column("created_at", DateTime(timezone=True)),
)

extraction_cache = Table(
    "extraction_cache", metadata,
    Column("key", String(200), primary_key=True),
    Column("result", Text),
    Column("created_at", DateTime(timezone=True)),
)


def now():
    return datetime.now(timezone.utc)


def _database_url():
    try:
        import streamlit as st
        if "DATABASE_URL" in st.secrets:
            return st.secrets["DATABASE_URL"]
    except Exception:
        pass
    return os.environ.get("DATABASE_URL") or "sqlite:///vendor_onboarding.db"


_engine = None
_engine_lock = threading.Lock()


def _create_tables(eng):
    """Create the tables the first time. Several visitors can open the app at the same moment,
    and each one may try this at once: on Postgres we take a lock first so exactly one of them
    does the creating, instead of two colliding half-way through."""
    try:
        if eng.dialect.name == "postgresql":
            with eng.begin() as conn:
                conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 727412})
                metadata.create_all(conn)
        else:
            metadata.create_all(eng)
    except (IntegrityError, ProgrammingError):
        # Belt and braces: if another worker still beat us to it, the tables now exist, which is
        # all we wanted. Confirm that, and only complain if something else is actually wrong.
        metadata.create_all(eng, checkfirst=True)


def engine():
    global _engine
    if _engine is None:
        with _engine_lock:                       # and the same guard within one worker
            if _engine is None:
                url = _database_url()
                # Hosted providers hand out URLs like "postgresql://user:pass@host/db". Name the
                # driver explicitly (psycopg 3), because different SQLAlchemy versions pick
                # different defaults for a bare "postgresql://" and the wrong one fails at connect.
                for prefix in ("postgres://", "postgresql://"):
                    if url.startswith(prefix):
                        url = "postgresql+psycopg://" + url[len(prefix):]
                        break
                eng = create_engine(url, pool_pre_ping=True)
                _create_tables(eng)
                _engine = eng                    # only published once the tables are really there
    return _engine


def using_hosted_db():
    return not _database_url().startswith("sqlite")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def save_submission(payload: dict, docs: list[dict], source: str, case_id=None, parent_id=None) -> int:
    """docs = [{slot, filename, mime_type, sha256, content(bytes)}]. Returns the new submission id."""
    with engine().begin() as conn:
        if parent_id:
            parent = conn.execute(select(submissions).where(submissions.c.id == parent_id)).mappings().one()
            ref, version = parent["request_ref"], parent["version"] + 1
        else:
            count = conn.execute(select(func.count()).select_from(submissions)
                                 .where(submissions.c.parent_id.is_(None))).scalar()
            ref, version = f"REQ-{count + 1:04d}", 1
        sid = conn.execute(insert(submissions).values(
            request_ref=ref, version=version, parent_id=parent_id,
            vendor_name=payload.get("company", {}).get("legal_name", ""),
            source=source, case_id=case_id, payload=json.dumps(payload), created_at=now(),
        )).inserted_primary_key[0]
        for d in docs:
            conn.execute(insert(documents).values(submission_id=sid, **d))
    return sid


def save_run(submission_id, started_at, status, summary, reason_codes, checks, extractions, email, vendor_id):
    finished = now()
    with engine().begin() as conn:
        return conn.execute(insert(runs).values(
            submission_id=submission_id, started_at=started_at, finished_at=finished,
            seconds=round((finished - started_at).total_seconds(), 1), status=status, summary=summary,
            reason_codes=json.dumps(reason_codes), checks=json.dumps(checks),
            extractions=json.dumps(extractions), email=json.dumps(email), vendor_id=vendor_id,
        )).inserted_primary_key[0]


def save_override(run_id, new_status, reason, reviewer):
    with engine().begin() as conn:
        conn.execute(insert(overrides).values(run_id=run_id, new_status=new_status, reason=reason,
                                              reviewer=reviewer, created_at=now()))


class Cache:
    """What the AI read off each file, keyed by the file's fingerprint + model name."""

    def get(self, key):
        with engine().connect() as conn:
            row = conn.execute(select(extraction_cache.c.result).where(extraction_cache.c.key == key)).first()
        return json.loads(row[0]) if row else None

    def put(self, key, result):
        with engine().begin() as conn:
            exists = conn.execute(select(extraction_cache.c.key).where(extraction_cache.c.key == key)).first()
            stmt = (update(extraction_cache).where(extraction_cache.c.key == key) if exists
                    else insert(extraction_cache).values(key=key))
            conn.execute(stmt.values(result=json.dumps(result), created_at=now()))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def _run_dict(r):
    d = dict(r)
    for k in ("reason_codes", "checks", "extractions", "email"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def get_submission(sid):
    with engine().connect() as conn:
        s = conn.execute(select(submissions).where(submissions.c.id == sid)).mappings().first()
        if not s:
            return None
        s = dict(s)
        s["payload"] = json.loads(s["payload"])
        s["documents"] = [dict(d) for d in conn.execute(
            select(documents).where(documents.c.submission_id == sid)).mappings()]
        s["runs"] = [_run_dict(r) for r in conn.execute(
            select(runs).where(runs.c.submission_id == sid).order_by(runs.c.id)).mappings()]
        for r in s["runs"]:
            r["overrides"] = [dict(o) for o in conn.execute(
                select(overrides).where(overrides.c.run_id == r["id"]).order_by(overrides.c.id)).mappings()]
        s["versions"] = [dict(v) for v in conn.execute(
            select(submissions.c.id, submissions.c.version, submissions.c.created_at)
            .where(submissions.c.request_ref == s["request_ref"]).order_by(submissions.c.version)).mappings()]
    return s


def final_status(run):
    """The latest human override wins; otherwise the process's own decision."""
    if run.get("overrides"):
        return run["overrides"][-1]["new_status"]
    return run["status"]


def list_runs():
    """One row per run, newest first, with its submission info and final status (for the dashboard)."""
    with engine().connect() as conn:
        rows = conn.execute(
            select(runs.c.id.label("run_id"), runs.c.status, runs.c.summary, runs.c.reason_codes,
                   runs.c.started_at, runs.c.seconds, runs.c.vendor_id,
                   submissions.c.id.label("submission_id"), submissions.c.request_ref,
                   submissions.c.version, submissions.c.vendor_name, submissions.c.source,
                   submissions.c.case_id, submissions.c.created_at)
            .join(submissions, runs.c.submission_id == submissions.c.id)
            .order_by(runs.c.id.desc())).mappings().all()
        ovr = conn.execute(select(overrides).order_by(overrides.c.id)).mappings().all()
    last_override = {}
    for o in ovr:
        last_override[o["run_id"]] = o
    out = []
    for r in rows:
        d = dict(r)
        d["reason_codes"] = json.loads(d["reason_codes"] or "[]")
        o = last_override.get(d["run_id"])
        d["final_status"] = o["new_status"] if o else d["status"]
        d["overridden"] = bool(o)
        out.append(d)
    return out


def approved_vendors():
    """Vendors approved through this app (real form submissions only, not demo test cases),
    in the same shape as existing_vendors.csv, for the duplicate check."""
    out = []
    for r in list_runs():
        if r["final_status"] == "Approved" and r["source"] == "form":
            p = get_submission(r["submission_id"])["payload"]
            out.append({"vendor_id": r["vendor_id"] or r["request_ref"], "legal_name": r["vendor_name"],
                        "pan": p["tax"].get("pan", ""), "gstin": p["tax"].get("gstin", ""),
                        "account_number": p["bank"].get("account_number", ""),
                        "ifsc": p["bank"].get("ifsc", ""), "request_ref": r["request_ref"]})
    return out


def latest_submission_for_case(case_id):
    with engine().connect() as conn:
        row = conn.execute(select(submissions.c.id).where(submissions.c.case_id == case_id)
                           .order_by(submissions.c.id.desc())).first()
    return row[0] if row else None


def next_vendor_id():
    with engine().connect() as conn:
        n = conn.execute(select(func.count()).select_from(runs).where(runs.c.vendor_id.is_not(None))).scalar()
    return f"VND-{101 + n:04d}"
