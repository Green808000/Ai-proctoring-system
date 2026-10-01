from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from models import db


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS team2_incident (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    first_event_exam_ms INTEGER NOT NULL,
    last_event_exam_ms INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    clip_path VARCHAR(500),
    clip_start_exam_ms INTEGER,
    clip_end_exam_ms INTEGER,
    created_at DATETIME NOT NULL,
    finalized_at DATETIME
);

CREATE INDEX IF NOT EXISTS ix_team2_incident_session_status
ON team2_incident(session_id, status);

CREATE TABLE IF NOT EXISTS team2_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    violation_id INTEGER,
    event_type VARCHAR(80) NOT NULL,
    exam_elapsed_ms INTEGER NOT NULL,
    video_offset_ms INTEGER,
    server_received_at DATETIME NOT NULL,
    FOREIGN KEY(incident_id) REFERENCES team2_incident(id),
    FOREIGN KEY(violation_id) REFERENCES violation(id)
);

CREATE INDEX IF NOT EXISTS ix_team2_event_incident
ON team2_event(incident_id, exam_elapsed_ms);

CREATE TABLE IF NOT EXISTS team2_recording_segment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    sequence_no INTEGER NOT NULL,
    start_exam_ms INTEGER NOT NULL,
    end_exam_ms INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    file_path VARCHAR(500) NOT NULL,
    mime_type VARCHAR(120),
    created_at DATETIME NOT NULL,
    UNIQUE(session_id, sequence_no)
);

CREATE INDEX IF NOT EXISTS ix_team2_segment_session_time
ON team2_recording_segment(session_id, start_exam_ms, end_exam_ms);

CREATE TABLE IF NOT EXISTS team2_recording_issue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    issue_type VARCHAR(80) NOT NULL,
    exam_elapsed_ms INTEGER,
    message VARCHAR(500),
    created_at DATETIME NOT NULL
);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_schema() -> None:
    """Create Team 2 tables without modifying the protected models.py schema."""
    for statement in SCHEMA_SQL.split(";"):
        statement = statement.strip()
        if statement:
            db.session.execute(text(statement))
    db.session.commit()


def _row_dict(row) -> dict[str, Any]:
    return dict(row._mapping)


def log_issue(session_id: int, issue_type: str, exam_elapsed_ms: int | None, message: str = "") -> None:
    db.session.execute(
        text(
            "INSERT INTO team2_recording_issue "
            "(session_id, issue_type, exam_elapsed_ms, message, created_at) "
            "VALUES (:session_id, :issue_type, :exam_elapsed_ms, :message, :created_at)"
        ),
        {
            "session_id": session_id,
            "issue_type": issue_type,
            "exam_elapsed_ms": exam_elapsed_ms,
            "message": message[:500],
            "created_at": _utc_now_iso(),
        },
    )
    db.session.commit()


def add_segment(
    session_id: int,
    sequence_no: int,
    start_exam_ms: int,
    end_exam_ms: int,
    duration_ms: int,
    file_path: str,
    mime_type: str | None,
) -> int:
    result = db.session.execute(
        text(
            "INSERT INTO team2_recording_segment "
            "(session_id, sequence_no, start_exam_ms, end_exam_ms, duration_ms, file_path, mime_type, created_at) "
            "VALUES (:session_id, :sequence_no, :start_exam_ms, :end_exam_ms, :duration_ms, :file_path, :mime_type, :created_at) "
            "ON CONFLICT(session_id, sequence_no) DO UPDATE SET "
            "start_exam_ms=excluded.start_exam_ms, end_exam_ms=excluded.end_exam_ms, "
            "duration_ms=excluded.duration_ms, file_path=excluded.file_path, mime_type=excluded.mime_type"
        ),
        {
            "session_id": session_id,
            "sequence_no": sequence_no,
            "start_exam_ms": start_exam_ms,
            "end_exam_ms": end_exam_ms,
            "duration_ms": duration_ms,
            "file_path": file_path,
            "mime_type": mime_type,
            "created_at": _utc_now_iso(),
        },
    )
    db.session.commit()
    row = db.session.execute(
        text("SELECT id FROM team2_recording_segment WHERE session_id=:s AND sequence_no=:q"),
        {"s": session_id, "q": sequence_no},
    ).first()
    return int(row[0])


def get_active_incident(session_id: int) -> dict[str, Any] | None:
    row = db.session.execute(
        text(
            "SELECT * FROM team2_incident WHERE session_id=:s AND status='active' "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"s": session_id},
    ).first()
    return _row_dict(row) if row else None


def record_event(
    session_id: int,
    violation_id: int | None,
    event_type: str,
    exam_elapsed_ms: int,
    server_received_at: str | None = None,
) -> dict[str, Any]:
    now = server_received_at or _utc_now_iso()
    active = get_active_incident(session_id)

    if active and exam_elapsed_ms - int(active["last_event_exam_ms"]) <= 10_000:
        incident_id = int(active["id"])
        first_event = int(active["first_event_exam_ms"])
    else:
        if active:
            db.session.execute(
                text(
                    "UPDATE team2_incident SET status='finalizing' WHERE id=:id AND status='active'"
                ),
                {"id": active["id"]},
            )
        result = db.session.execute(
            text(
                "INSERT INTO team2_incident "
                "(session_id, first_event_exam_ms, last_event_exam_ms, status, created_at) "
                "VALUES (:session_id, :first_event, :last_event, 'active', :created_at)"
            ),
            {
                "session_id": session_id,
                "first_event": exam_elapsed_ms,
                "last_event": exam_elapsed_ms,
                "created_at": now,
            },
        )
        incident_id = int(result.lastrowid)
        db.session.commit()
        first_event = exam_elapsed_ms

    db.session.execute(
        text(
            "UPDATE team2_incident SET last_event_exam_ms=:last_event, status='active' WHERE id=:id"
        ),
        {"last_event": exam_elapsed_ms, "id": incident_id},
    )
    db.session.execute(
        text(
            "INSERT INTO team2_event "
            "(incident_id, violation_id, event_type, exam_elapsed_ms, server_received_at) "
            "VALUES (:incident_id, :violation_id, :event_type, :exam_elapsed_ms, :server_received_at)"
        ),
        {
            "incident_id": incident_id,
            "violation_id": violation_id,
            "event_type": event_type.upper(),
            "exam_elapsed_ms": exam_elapsed_ms,
            "server_received_at": now,
        },
    )
    db.session.commit()

    return {"incident_id": incident_id, "first_event_exam_ms": first_event}


def list_segments_for_incident(session_id: int, first_event_ms: int, last_event_ms: int) -> list[dict[str, Any]]:
    start_ms = max(0, first_event_ms - 10_000)
    end_ms = last_event_ms + 10_000
    rows = db.session.execute(
        text(
            "SELECT * FROM team2_recording_segment WHERE session_id=:s "
            "AND end_exam_ms >= :start_ms AND start_exam_ms <= :end_ms "
            "ORDER BY sequence_no"
        ),
        {"s": session_id, "start_ms": start_ms, "end_ms": end_ms},
    ).fetchall()
    return [_row_dict(r) for r in rows]


def get_incident(incident_id: int) -> dict[str, Any] | None:
    row = db.session.execute(
        text("SELECT * FROM team2_incident WHERE id=:id"), {"id": incident_id}
    ).first()
    return _row_dict(row) if row else None


def get_events(incident_id: int) -> list[dict[str, Any]]:
    rows = db.session.execute(
        text(
            "SELECT * FROM team2_event WHERE incident_id=:id ORDER BY exam_elapsed_ms, id"
        ),
        {"id": incident_id},
    ).fetchall()
    return [_row_dict(r) for r in rows]


def get_incidents(session_id: int) -> list[dict[str, Any]]:
    rows = db.session.execute(
        text(
            "SELECT * FROM team2_incident WHERE session_id=:s ORDER BY first_event_exam_ms"
        ),
        {"s": session_id},
    ).fetchall()
    return [_row_dict(r) for r in rows]



def delete_session_data(session_id: int) -> list[str]:
    """Delete Team 2 database rows for a session and return referenced media paths.

    This is intentionally separate from the ExamSession ORM model because Team 2
    uses its own SQLite tables. Event rows are deleted before incidents so the
    foreign-key relationship remains valid when SQLite foreign keys are enabled.
    """
    init_schema()

    incident_rows = db.session.execute(
        text("SELECT id, clip_path FROM team2_incident WHERE session_id=:s"),
        {"s": session_id},
    ).fetchall()
    incident_ids = [int(row[0]) for row in incident_rows]
    media_paths = [str(row[1]) for row in incident_rows if row[1]]

    if incident_ids:
        placeholders = ",".join(f":id{i}" for i in range(len(incident_ids)))
        params = {f"id{i}": value for i, value in enumerate(incident_ids)}
        db.session.execute(
            text(f"DELETE FROM team2_event WHERE incident_id IN ({placeholders})"),
            params,
        )
        db.session.execute(
            text(f"DELETE FROM team2_incident WHERE id IN ({placeholders})"),
            params,
        )

    segment_rows = db.session.execute(
        text("SELECT file_path FROM team2_recording_segment WHERE session_id=:s"),
        {"s": session_id},
    ).fetchall()
    media_paths.extend(str(row[0]) for row in segment_rows if row[0])

    db.session.execute(
        text("DELETE FROM team2_recording_segment WHERE session_id=:s"),
        {"s": session_id},
    )
    db.session.execute(
        text("DELETE FROM team2_recording_issue WHERE session_id=:s"),
        {"s": session_id},
    )

    return media_paths

def attach_event_offsets(incident_id: int, segment_rows: list[dict[str, Any]], clip_start_exam_ms: int, duration_overrides: dict[int, float] | None = None) -> None:
    if not segment_rows:
        return
    events = get_events(incident_id)
    for event in events:
        event_ms = int(event["exam_elapsed_ms"])
        offset = 0
        found = False
        for seg in segment_rows:
            start = int(seg["start_exam_ms"])
            end = int(seg["end_exam_ms"])
            override_seconds = (duration_overrides or {}).get(int(seg["sequence_no"]))
            duration_ms = (max(0.0, float(override_seconds)) * 1000.0) if override_seconds is not None else float(max(0, int(seg["duration_ms"])))
            if start <= event_ms <= end:
                span = max(1, end - start)
                ratio = max(0.0, min(1.0, (event_ms - start) / span))
                offset += duration_ms * ratio
                found = True
                break
            if end < event_ms:
                offset += duration_ms
        if not found:
            offset = max(0.0, float(event_ms - clip_start_exam_ms))
        db.session.execute(
            text("UPDATE team2_event SET video_offset_ms=:offset WHERE id=:id"),
            {"offset": int(round(offset)), "id": event["id"]},
        )
    db.session.commit()
