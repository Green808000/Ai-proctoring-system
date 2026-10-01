from flask import Blueprint, request, jsonify, current_app
from datetime import datetime
from pathlib import Path
import shutil
from models import db, Exam, ExamEnrollment, ExamSession, Violation, VerificationAttempt, User
from team2.store import init_schema as init_team2_schema, get_incidents, delete_session_data
from team2.media import finalize_incident
from session_cleanup import mark_stale_sessions

exam_bp = Blueprint('exam', __name__)


def _duration_seconds(start_time, end_time=None):
    if not start_time:
        return 0
    end = end_time or datetime.utcnow()
    try:
        return max(0, int((end - start_time).total_seconds()))
    except Exception:
        return 0


def _format_duration(total_seconds):
    total = max(0, int(total_seconds or 0))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _profile_url(student):
    if not student or not student.id_photo_path:
        return None
    path = str(student.id_photo_path).replace('\\', '/').lstrip('/')
    if path.startswith('static/'):
        path = path[7:]
    return f"/static/{path}"


@exam_bp.route('/create', methods=['POST'])
def create_exam():
    data = request.get_json()
    exam = Exam(
        course_code=data['course_code'],
        course_title=data['course_title'],
        start_time=datetime.fromisoformat(data['start_time']),
        time_hours=data['time_hours'],
        created_by=data.get('admin_id')
    )
    db.session.add(exam)
    db.session.commit()

    for student_id in data['student_ids']:
        db.session.add(ExamEnrollment(exam_id=exam.id, student_id=student_id))
    db.session.commit()

    return jsonify({"status": "success", "exam_id": exam.id}), 201


@exam_bp.route('/start-session', methods=['POST'])
def start_session():
    data = request.get_json()
    session = ExamSession(
        exam_id=data['exam_id'],
        student_id=data['student_id'],
        last_heartbeat=datetime.utcnow(),
    )
    db.session.add(session)
    db.session.commit()
    return jsonify({"status": "success", "session_id": session.id})


@exam_bp.route('/end-session/<int:session_id>', methods=['POST'])
def end_session(session_id):
    session = ExamSession.query.get_or_404(session_id)
    session.end_time = datetime.utcnow()
    session.status = 'completed'
    db.session.commit()

    try:
        init_team2_schema()
        for incident in get_incidents(session_id):
            if incident.get('status') in ('active', 'finalizing'):
                try:
                    finalize_incident(int(incident['id']))
                except Exception as exc:
                    print(f"Team 2 incident {incident['id']} finalization deferred: {exc}")
    except Exception as exc:
        print(f"Team 2 finalization check failed: {exc}")

    return jsonify({"status": "success"})



def _safe_media_file(root: Path, stored_path: str | None) -> Path | None:
    """Resolve a stored media path only if it stays inside the application root."""
    if not stored_path:
        return None
    raw = str(stored_path).replace("\\", "/").lstrip("/")
    if raw.startswith("static/"):
        candidate = root / raw
    else:
        candidate = root / "static" / raw
    try:
        candidate = candidate.resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def _remove_session_media(session_id: int, stored_paths: list[str]) -> None:
    root = Path(current_app.root_path)
    paths = []

    for stored_path in stored_paths:
        candidate = _safe_media_file(root, stored_path)
        if candidate:
            paths.append(candidate)

    # Clean any files left by Team 2 even if their database row was lost.
    paths.extend([
        root / "static" / "incident_segments" / f"session_{session_id}",
        root / "static" / "incident_clips" / f"session_{session_id}",
    ])

    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                path.unlink(missing_ok=True)
        except OSError as exc:
            current_app.logger.warning("Could not remove session media %s: %s", path, exc)


@exam_bp.route('/admin/session/<int:session_id>/delete', methods=['POST'])
def delete_session(session_id):
    session = ExamSession.query.get_or_404(session_id)

    # Do not delete a live session while the student may still be recording.
    if str(session.status or '').lower() == 'in progress':
        return jsonify({
            "error": "This session is still in progress. End the session before deleting it."
        }), 409

    # Collect all file references before deleting their database rows.
    stored_paths = []
    if session.video_path:
        stored_paths.append(session.video_path)
    if session.audio_path:
        stored_paths.append(session.audio_path)

    violations = Violation.query.filter_by(session_id=session.id).all()
    stored_paths.extend(v.snapshot_path for v in violations if v.snapshot_path)
    stored_paths.extend(v.clip_path for v in violations if v.clip_path)

    attempts = VerificationAttempt.query.filter_by(session_id=session.id).all()
    stored_paths.extend(a.live_snapshot_path for a in attempts if a.live_snapshot_path)

    try:
        init_team2_schema()
        stored_paths.extend(delete_session_data(session.id))

        # Delete SQLAlchemy rows in dependency order.
        VerificationAttempt.query.filter_by(session_id=session.id).delete(synchronize_session=False)
        Violation.query.filter_by(session_id=session.id).delete(synchronize_session=False)
        db.session.delete(session)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Failed to delete exam session %s", session_id)
        return jsonify({"error": f"Could not delete session: {exc}"}), 500

    # Only remove files after the database transaction succeeds.
    _remove_session_media(session_id, stored_paths)

    # Stop any in-memory proctor/liveness state associated with the deleted session.
    try:
        from routes.proctor import active_engines, liveness_challenges
        active_engines.pop(session_id, None)
        liveness_challenges.pop(session_id, None)
    except Exception:
        pass

    return jsonify({"status": "success", "message": f"Session {session_id} deleted"})

@exam_bp.route('/my-exams/<student_id>')
def my_exams(student_id):
    enrollments = ExamEnrollment.query.filter_by(student_id=student_id).all()
    if not enrollments:
        return jsonify([])

    exam_ids = [e.exam_id for e in enrollments]
    exams = Exam.query.filter(Exam.id.in_(exam_ids)).all()

    return jsonify([{
        "exam_id": e.id,
        "course_code": e.course_code,
        "course_title": e.course_title,
        "start_time": e.start_time.isoformat(),
        "time_hours": e.time_hours
    } for e in exams])


@exam_bp.route('/admin/sessions')
def admin_sessions():
    # Make sure abandoned sessions are shown as disconnected even if the
    # background sweeper is not running (e.g. under gunicorn).
    try:
        mark_stale_sessions()
    except Exception as exc:
        db.session.rollback()
        print(f"Stale-session check failed: {exc}")

    sessions = ExamSession.query.order_by(ExamSession.start_time.desc()).all()

    result = []
    for s in sessions:
        student = User.query.filter_by(student_id=s.student_id).first()
        exam = Exam.query.get(s.exam_id)
        violation_count = Violation.query.filter_by(session_id=s.id).count()
        duration_seconds = _duration_seconds(s.start_time, s.end_time)

        result.append({
            "session_id": s.id,
            "exam_id": s.exam_id,
            "student_id": s.student_id,
            "student_name": student.name if student else "Unknown",
            "student_email": student.email if student else "Unknown",
            "profile_url": _profile_url(student),
            "subject": exam.course_title if exam else "Unknown subject",
            "course_code": exam.course_code if exam else "—",
            "exam_duration_hours": exam.time_hours if exam else None,
            "status": s.status,
            "start_time": s.start_time.isoformat(),
            "end_time": s.end_time.isoformat() if s.end_time else None,
            "duration_seconds": duration_seconds,
            "duration_display": _format_duration(duration_seconds),
            "violation_count": violation_count
        })

    return jsonify(result)


@exam_bp.route('/admin/session/<int:session_id>')
def admin_session_detail(session_id):
    session = ExamSession.query.get_or_404(session_id)
    student = User.query.filter_by(student_id=session.student_id).first()
    exam = Exam.query.get(session.exam_id)
    duration_seconds = _duration_seconds(session.start_time, session.end_time)

    return jsonify({
        "session_id": session.id,
        "student_id": session.student_id,
        "student_name": student.name if student else "Unknown",
        "student_email": student.email if student else "Unknown",
        "profile_url": _profile_url(student),
        "subject": exam.course_title if exam else "Unknown subject",
        "course_code": exam.course_code if exam else "—",
        "status": session.status,
        "start_time": session.start_time.isoformat(),
        "end_time": session.end_time.isoformat() if session.end_time else None,
        "duration_seconds": duration_seconds,
        "duration_display": _format_duration(duration_seconds),
        "violation_count": Violation.query.filter_by(session_id=session.id).count()
    })


@exam_bp.route('/admin/session/<int:session_id>/violations')
def admin_session_violations(session_id):
    violations = Violation.query.filter_by(session_id=session_id).order_by(Violation.timestamp).all()

    return jsonify([{
        "event_type": v.event_type,
        "severity": v.severity,
        "confidence": v.confidence,
        "timestamp": v.timestamp.strftime("%H:%M:%S"),
        "snapshot_url": f"{request.host_url.rstrip('/')}/{v.snapshot_path}" if v.snapshot_path else None
    } for v in violations])


@exam_bp.route('/my-exams-detailed/<student_id>')
def my_exams_detailed(student_id):
    enrollments = ExamEnrollment.query.filter_by(student_id=student_id).all()
    exam_ids = [e.exam_id for e in enrollments]
    exams = Exam.query.filter(Exam.id.in_(exam_ids)).all()

    now = datetime.now()
    upcoming = []
    previous = []

    for e in exams:
        session = ExamSession.query.filter_by(exam_id=e.id, student_id=student_id).first()

        exam_data = {
            "exam_id": e.id,
            "course_code": e.course_code,
            "course_title": e.course_title,
            "start_time": e.start_time.isoformat(),
            "time_hours": e.time_hours,
            "session_status": session.status if session else "not_started"
        }

        if session and session.status == "completed":
            previous.append(exam_data)
        elif e.start_time < now and not session:
            previous.append(exam_data)
        else:
            upcoming.append(exam_data)

    return jsonify({"upcoming": upcoming, "previous": previous})
