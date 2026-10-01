from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request, url_for, current_app
from models import db, ExamSession
from sqlalchemy import text
from werkzeug.utils import secure_filename

from team2.store import (
    add_segment,
    get_events,
    get_incident,
    get_incidents,
    init_schema,
    log_issue,
    record_event,
)
from team2.media import finalize_incident

team2_bp = Blueprint("team2", __name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_session(session_id):
    session = ExamSession.query.get(session_id)
    if not session:
        return None
    return session


@team2_bp.route("/health", methods=["GET"])
def health():
    init_schema()
    return jsonify({"status": "ok", "component": "team2"})


@team2_bp.route("/recording/segment", methods=["POST"])
def upload_segment():
    init_schema()
    try:
        session_id = int(request.form.get("session_id", ""))
        sequence_no = int(request.form.get("sequence_no", ""))
        start_exam_ms = int(request.form.get("start_exam_ms", ""))
        end_exam_ms = int(request.form.get("end_exam_ms", ""))
        duration_ms = int(request.form.get("duration_ms", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid recording segment metadata"}), 400

    if end_exam_ms <= start_exam_ms or duration_ms <= 0:
        return jsonify({"error": "Invalid recording segment timing"}), 400
    if sequence_no < 0:
        return jsonify({"error": "Invalid segment sequence"}), 400
    if not _require_session(session_id):
        return jsonify({"error": "Exam session not found"}), 404
    if "segment" not in request.files:
        return jsonify({"error": "No segment file provided"}), 400

    segment_file = request.files["segment"]
    if not segment_file.filename:
        return jsonify({"error": "Empty segment filename"}), 400

    root = Path(current_app.root_path)
    out_dir = root / "static" / "incident_segments" / f"session_{session_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = secure_filename(segment_file.filename) or "segment.webm"
    filename = f"segment_{sequence_no:08d}_{uuid.uuid4().hex[:10]}_{safe}"
    save_path = out_dir / filename
    segment_file.save(save_path)

    if save_path.stat().st_size == 0:
        save_path.unlink(missing_ok=True)
        return jsonify({"error": "Empty recording segment"}), 400

    rel_path = str(save_path.relative_to(root)).replace("\\", "/")
    try:
        segment_id = add_segment(
            session_id=session_id,
            sequence_no=sequence_no,
            start_exam_ms=start_exam_ms,
            end_exam_ms=end_exam_ms,
            duration_ms=duration_ms,
            file_path=str(save_path),
            mime_type=request.form.get("mime_type"),
        )
    except Exception as exc:
        db.session.rollback()
        save_path.unlink(missing_ok=True)
        return jsonify({"error": f"Could not store recording segment: {exc}"}), 500

    return jsonify({
        "status": "stored",
        "segment_id": segment_id,
        "sequence_no": sequence_no,
        "path": rel_path,
    }), 201


@team2_bp.route("/recording/issue", methods=["POST"])
def recording_issue():
    init_schema()
    data = request.get_json(silent=True) or {}
    try:
        session_id = int(data.get("session_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid session_id"}), 400
    if not _require_session(session_id):
        return jsonify({"error": "Exam session not found"}), 404
    try:
        exam_elapsed_ms = int(data["exam_elapsed_ms"]) if data.get("exam_elapsed_ms") is not None else None
    except (TypeError, ValueError):
        exam_elapsed_ms = None
    try:
        log_issue(session_id, str(data.get("issue_type", "UNKNOWN"))[:80], exam_elapsed_ms, str(data.get("message", "")))
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 500
    return jsonify({"status": "logged"}), 201


@team2_bp.route("/events", methods=["POST"])
def team2_event():
    init_schema()
    data = request.get_json(silent=True) or {}
    try:
        session_id = int(data.get("session_id"))
        exam_elapsed_ms = int(data.get("exam_elapsed_ms"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid session_id or exam_elapsed_ms"}), 400

    if not _require_session(session_id):
        return jsonify({"error": "Exam session not found"}), 404

    violation_id = data.get("violation_id")
    if violation_id is not None:
        try:
            violation_id = int(violation_id)
        except (TypeError, ValueError):
            violation_id = None

    try:
        result = record_event(
            session_id=session_id,
            violation_id=violation_id,
            event_type=str(data.get("event_type", "UNKNOWN")),
            exam_elapsed_ms=exam_elapsed_ms,
            server_received_at=_now_iso(),
        )
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": f"Could not record Team 2 event: {exc}"}), 500

    return jsonify({"status": "recorded", **result}), 201


@team2_bp.route("/incidents/<int:incident_id>/finalize", methods=["POST"])
def finalize(incident_id):
    init_schema()
    incident = get_incident(incident_id)
    if not incident:
        return jsonify({"error": "Incident not found"}), 404
    data = request.get_json(silent=True) or {}
    force_end = bool(data.get("force_end", False))
    try:
        exam_end_ms = int(data["exam_end_ms"]) if data.get("exam_end_ms") is not None else None
    except (TypeError, ValueError):
        exam_end_ms = None
    try:
        finalized = finalize_incident(incident_id, force_end=force_end, exam_end_ms=exam_end_ms)
    except RuntimeError as exc:
        return jsonify({"status": "waiting", "message": str(exc)}), 409
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": f"Incident finalization failed: {exc}"}), 500
    return jsonify({"status": "finalized", "incident": _incident_payload(finalized)})


@team2_bp.route("/incidents/session/<int:session_id>", methods=["GET"])
def session_incidents(session_id):
    init_schema()
    if not _require_session(session_id):
        return jsonify({"error": "Exam session not found"}), 404
    return jsonify({"incidents": [_incident_payload(x, include_events=True) for x in get_incidents(session_id)]})


@team2_bp.route("/incidents/<int:incident_id>", methods=["GET"])
def incident_detail(incident_id):
    init_schema()
    incident = get_incident(incident_id)
    if not incident:
        return jsonify({"error": "Incident not found"}), 404
    return jsonify(_incident_payload(incident, include_events=True))


def _format_clock(ms: int | None) -> str:
    if ms is None:
        return "--:--"
    total = max(0, int(ms)) // 1000
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _incident_payload(incident: dict, include_events: bool = False) -> dict:
    payload = dict(incident)
    payload["start_display"] = _format_clock(incident.get("first_event_exam_ms"))
    payload["end_display"] = _format_clock(incident.get("last_event_exam_ms"))
    payload["clip_url"] = None
    if incident.get("clip_path"):
        rel = str(incident["clip_path"])
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        payload["clip_url"] = url_for("static", filename=rel)
    if include_events:
        events = []
        for event in get_events(int(incident["id"])):
            e = dict(event)
            e["display_time"] = _format_clock(e.get("exam_elapsed_ms"))
            events.append(e)
        payload["events"] = events
    return payload
