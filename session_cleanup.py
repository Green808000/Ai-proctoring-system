"""Closes exam sessions that were abandoned (tab closed, browser crash, lost network).

Why this exists
---------------
A session used to be closed only when the student pressed "Submit exam"
(POST /api/exam/end-session/<id>). Closing the tab, crashing, or losing
the connection never triggered that call, so the session stayed
"In Progress" forever and its incidents stayed "active" (never turned
into a video clip).

How it works
------------
1. Heartbeat: every /scan-frame and /calibrate request calls
   touch_session(), which stamps ExamSession.last_heartbeat. The exam page
   already posts a frame every 3 seconds, so no frontend change is needed.
2. Sweep: mark_stale_sessions() finds "In Progress" sessions whose last
   heartbeat is older than SESSION_HEARTBEAT_TIMEOUT_SECONDS and marks them
   "disconnected", with end_time set to the last time we actually heard
   from the student (not "now").
3. Incident clean-up: finalize_orphaned_incidents() builds the video clip
   for any incident that belongs to a session that is no longer in
   progress, using the segments that were uploaded before the drop.
4. A background thread runs 2 and 3 every SESSION_SWEEP_INTERVAL_SECONDS.
   mark_stale_sessions() is also called when the admin loads the session
   list, so the dashboard is correct even if the thread is not running.

A "disconnected" session is not final. If the same session sends another
heartbeat (refresh, network came back, a backgrounded tab woke up) it
flips back to "In Progress".
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import text

from models import db, ExamSession, Violation

log = logging.getLogger(__name__)

IN_PROGRESS = "In Progress"
COMPLETED = "completed"
DISCONNECTED = "disconnected"

DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_SWEEP_INTERVAL_SECONDS = 30
MAX_FINALIZE_FAILURES = 3

# incident_id -> number of failed clip builds (in memory; resets on restart)
_finalize_failures = {}


# ---------------------------------------------------------------------------
# Pure helpers (no database) -- easy to unit test
# ---------------------------------------------------------------------------

def is_stale(last_heartbeat, start_time, now, timeout_seconds):
    """True if we have not heard from a session for longer than the timeout.

    Sessions created before this feature existed have no heartbeat, so
    their start time is used instead.
    """
    reference = last_heartbeat or start_time
    if reference is None:
        return False
    return (now - reference) > timedelta(seconds=timeout_seconds)


def estimate_last_seen(last_heartbeat, start_time, last_violation_time):
    """Best guess for when the student was last actually present."""
    if last_heartbeat is not None:
        return last_heartbeat
    candidates = [t for t in (start_time, last_violation_time) if t is not None]
    return max(candidates) if candidates else None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_schema():
    """Add exam_session.last_heartbeat to databases created before this feature.

    db.create_all() creates missing tables but never alters existing ones,
    so an existing proctoring.db needs this one-off ALTER. Safe to call on
    every start-up.
    """
    columns = {row[1] for row in db.session.execute(text("PRAGMA table_info(exam_session)"))}
    if "last_heartbeat" not in columns:
        db.session.execute(text("ALTER TABLE exam_session ADD COLUMN last_heartbeat DATETIME"))
        db.session.commit()
        log.info("Added exam_session.last_heartbeat column")


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def touch_session(session_id, now=None):
    """Record that the student's page is still talking to us.

    Never raises: a heartbeat problem must not break frame scanning.
    """
    try:
        session = ExamSession.query.get(session_id)
        if session is None:
            return None
        session.last_heartbeat = now or datetime.utcnow()
        if session.status == DISCONNECTED:
            # The student came back -- the earlier disconnect was not final.
            session.status = IN_PROGRESS
            session.end_time = None
        db.session.commit()
        return session
    except Exception:
        db.session.rollback()
        log.exception("Could not record heartbeat for session %s", session_id)
        return None


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def mark_stale_sessions(timeout_seconds=None, now=None):
    """Mark silent "In Progress" sessions as disconnected. Returns their ids."""
    from flask import current_app

    if timeout_seconds is None:
        timeout_seconds = current_app.config.get(
            "SESSION_HEARTBEAT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=timeout_seconds)

    candidates = ExamSession.query.filter(
        ExamSession.status == IN_PROGRESS,
        db.func.coalesce(ExamSession.last_heartbeat, ExamSession.start_time) < cutoff,
    ).all()

    closed = []
    for session in candidates:
        if not is_stale(session.last_heartbeat, session.start_time, now, timeout_seconds):
            continue
        last_violation = db.session.query(db.func.max(Violation.timestamp)).filter(
            Violation.session_id == session.id).scalar()
        session.status = DISCONNECTED
        session.end_time = estimate_last_seen(
            session.last_heartbeat, session.start_time, last_violation)
        closed.append(session.id)

    if closed:
        db.session.commit()
        log.info("Marked %d abandoned session(s) as disconnected: %s", len(closed), closed)
    return closed


def finalize_orphaned_incidents(max_per_run=3):
    """Build clips for incidents whose session is no longer in progress.

    Clip building runs FFmpeg and can be slow, so only a few incidents are
    handled per sweep; the rest are picked up on the next one.
    """
    from team2.media import finalize_incident

    rows = db.session.execute(
        text(
            "SELECT i.id FROM team2_incident i "
            "JOIN exam_session s ON s.id = i.session_id "
            "WHERE i.status IN ('active', 'finalizing') AND s.status != :in_progress "
            "ORDER BY i.id LIMIT :n"
        ),
        {"in_progress": IN_PROGRESS, "n": max_per_run},
    ).fetchall()

    done = []
    for (incident_id,) in rows:
        try:
            finalize_incident(int(incident_id), force_end=True)
            _finalize_failures.pop(incident_id, None)
            done.append(incident_id)
        except Exception as exc:
            db.session.rollback()
            message = str(exc)
            if "FFmpeg was not found" in message:
                # Environment problem, not the incident's fault: leave it
                # alone so it succeeds once FFmpeg is installed.
                log.warning("Cannot finalize incident %s: %s", incident_id, message)
                continue
            if "No recording segments" in message or "No valid recording segments" in message:
                _set_incident_status(incident_id, "unavailable")
                log.info("Incident %s has no recording segments; marked unavailable", incident_id)
                continue
            _finalize_failures[incident_id] = _finalize_failures.get(incident_id, 0) + 1
            if _finalize_failures[incident_id] >= MAX_FINALIZE_FAILURES:
                _set_incident_status(incident_id, "failed")
                log.error("Incident %s failed %d times; marked failed: %s",
                          incident_id, MAX_FINALIZE_FAILURES, message)
            else:
                log.warning("Incident %s finalize attempt failed: %s", incident_id, message)
    return done


def _set_incident_status(incident_id, status):
    db.session.execute(
        text("UPDATE team2_incident SET status=:s WHERE id=:id"),
        {"s": status, "id": incident_id},
    )
    db.session.commit()


def sweep_once():
    mark_stale_sessions()
    finalize_orphaned_incidents()


def start_sweeper(app):
    """Start the background sweep thread. Call once, from the process that serves requests."""
    interval = app.config.get("SESSION_SWEEP_INTERVAL_SECONDS", DEFAULT_SWEEP_INTERVAL_SECONDS)

    def loop():
        while True:
            try:
                with app.app_context():
                    sweep_once()
            except Exception:
                log.exception("Session sweep failed")
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="session-sweeper", daemon=True)
    thread.start()
    log.info("Session sweeper started (every %ss)", interval)
    return thread
