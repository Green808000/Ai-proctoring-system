import cv2
import numpy as np
import base64
import os
import random
from datetime import datetime
from flask import Blueprint, request, jsonify
from models import db, Violation, ExamSession
from detection.engine import ProctoringEngine
from werkzeug.utils import secure_filename
from detection.face_detection import detect_faces
from detection.face_authentication import (
    build_embedding, verify_face_deep, get_or_load_signature, get_deepface_threshold,
    new_blink_challenge_state, check_blink_challenge,
    new_head_turn_challenge_state, check_head_turn_challenge,
)
from models import VerificationAttempt
from team2.store import record_event as team2_record_event, init_schema as init_team2_schema
from session_cleanup import touch_session


proctor_bp = Blueprint('proctor', __name__)

active_engines = {}

liveness_challenges = {}
LIVENESS_INSTRUCTIONS = {
    "blink": "Please blink to continue",
    "head_turn": {
        "left": "Start facing the center. Turn your head to YOUR LEFT (toward the right side of the screen), then return to the center.",
        "right": "Start facing the center. Turn your head to YOUR RIGHT (toward the left side of the screen), then return to the center.",
    },
}

def _liveness_instruction(challenge_type, direction):
    if challenge_type == "blink":
        return LIVENESS_INSTRUCTIONS["blink"]
    return LIVENESS_INSTRUCTIONS["head_turn"][direction]

def _new_challenge():
    challenge_type = random.choice(["blink", "head_turn"])
    direction = random.choice(["left", "right"]) if challenge_type == "head_turn" else None
    state = new_blink_challenge_state() if challenge_type == "blink" else new_head_turn_challenge_state()
    return {"type": challenge_type, "direction": direction, "state": state, "passed": False}

def _get_or_create_engine(session_id):
    if session_id not in active_engines:
        exam_session = ExamSession.query.get(session_id)
        student_id = exam_session.student_id if exam_session else None
        active_engines[session_id] = ProctoringEngine(student_id=student_id)
    return active_engines[session_id]


@proctor_bp.route('/scan-frame', methods=['POST'])
def scan_frame():
    data = request.get_json()
    if not data or 'session_id' not in data or 'image' not in data:
        return jsonify({"error": "Missing session_id or image payload"}), 400

    try:
        session_id = int(data['session_id'])
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid session_id format"}), 400

    try:
        exam_elapsed_ms = int(data.get('exam_elapsed_ms')) if data.get('exam_elapsed_ms') is not None else None
    except (ValueError, TypeError):
        exam_elapsed_ms = None

    init_team2_schema()
    touch_session(session_id)  # heartbeat: proves the exam page is still alive
    engine = _get_or_create_engine(session_id)

    try:
        image_b64 = data['image'].split(
            ',')[1] if ',' in data['image'] else data['image']
        nparr = np.frombuffer(base64.b64decode(image_b64), np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if frame is None:
            return jsonify({"error": "Failed to decode frame"}), 400

        result = engine.process_frame(frame)

        logged_count = 0
        new_violation_ids = []
        team2_events = []

        if result.get('events'):
            for event in result['events']:
                event_name = event.get('event_type') if isinstance(
                    event, dict) else str(event)
                severity_val = event.get('severity', 'high') if isinstance(
                    event, dict) else 'high'
                confidence_val = event.get(
                    'confidence', None) if isinstance(event, dict) else None

                if event_name:
                    os.makedirs("static/violation_snapshots", exist_ok=True)
                    snapshot_filename = f"{session_id}_{event_name}_{int(datetime.utcnow().timestamp())}.jpg"
                    snapshot_path = f"static/violation_snapshots/{snapshot_filename}"
                    cv2.imwrite(snapshot_path, frame)

                    violation = Violation(
                        session_id=session_id,
                        event_type=str(event_name).upper(),
                        severity=severity_val,
                        confidence=confidence_val,
                        snapshot_path=snapshot_path
                    )
                    db.session.add(violation)
                    db.session.flush()
                    new_violation_ids.append(
                        {"id": violation.id, "event_type": violation.event_type})
                    if exam_elapsed_ms is not None:
                        team2_result = team2_record_event(
                            session_id=session_id,
                            violation_id=violation.id,
                            event_type=violation.event_type,
                            exam_elapsed_ms=exam_elapsed_ms,
                        )
                        team2_events.append({
                            "violation_id": violation.id,
                            "event_type": violation.event_type,
                            "exam_elapsed_ms": exam_elapsed_ms,
                            **team2_result,
                        })
                    logged_count += 1

            db.session.commit()
            print(
                f" SUCCESS: Saved {logged_count} violations for Session ID {session_id}")

        return jsonify({
            "face_count": result.get('face_count', 0),
            "events_logged": logged_count,
            "calibrating": result.get('calibrating', False),
            "calibration_done": result.get('calibration_done', False),
            "baseline_ready": result.get('baseline_ready', False),
            "new_violations": new_violation_ids,
            "team2_events": team2_events,
            "identity_check": result.get("identity_check")
        })

    except Exception as e:
        db.session.rollback()
        print(f" ERROR inside /scan-frame: {str(e)}")
        return jsonify({"error": f"Frame scanning failed: {str(e)}"}), 500


@proctor_bp.route('/calibrate', methods=['POST'])
def start_calibration():
    data = request.get_json()
    if not data or 'session_id' not in data:
        return jsonify({"error": "Missing session_id"}), 400

    try:
        session_id = int(data['session_id'])
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid session_id"}), 400

    touch_session(session_id)
    engine = _get_or_create_engine(session_id)

    engine.start_calibration()
    return jsonify({"status": "calibration_started", "session_id": session_id})


@proctor_bp.route('/check-lighting', methods=['POST'])
def check_lighting():
    data = request.get_json()
    if not data or 'image' not in data:
        return jsonify({"error": "Missing image"}), 400

    try:
        image_b64 = data['image'].split(
            ',')[1] if ',' in data['image'] else data['image']
        nparr = np.frombuffer(base64.b64decode(image_b64), np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if frame is None:
            return jsonify({"error": "Failed to decode frame"}), 400

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness = float(np.mean(gray))

        MIN_BRIGHTNESS = 60

        return jsonify({
            "brightness": round(brightness, 1),
            "well_lit": brightness >= MIN_BRIGHTNESS
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@proctor_bp.route('/flag', methods=['POST'])
def log_client_flag():
    data = request.get_json()
    if not data or 'session_id' not in data or 'event_type' not in data:
        return jsonify({"error": "Missing session_id or event_type"}), 400

    try:
        session_id = int(data['session_id'])
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid session_id"}), 400

    try:
        exam_elapsed_ms = int(data.get('exam_elapsed_ms')) if data.get('exam_elapsed_ms') is not None else None
    except (ValueError, TypeError):
        exam_elapsed_ms = None

    init_team2_schema()

    violation = Violation(
        session_id=session_id,
        event_type=data['event_type'].upper(),
        severity=data.get('severity', 'high'),
        confidence=None
    )
    db.session.add(violation)
    db.session.flush()

    team2_event = None
    if exam_elapsed_ms is not None:
        team2_event = team2_record_event(
            session_id=session_id,
            violation_id=violation.id,
            event_type=violation.event_type,
            exam_elapsed_ms=exam_elapsed_ms,
        )
    db.session.commit()

    print(
        f" SUCCESS: Logged client-side flag '{data['event_type']}' for session {session_id}")

    return jsonify({"status": "logged", "violation_id": violation.id, "team2_event": team2_event}), 201


@proctor_bp.route('/upload-clip/<int:violation_id>', methods=['POST'])
def upload_clip(violation_id):
    violation = Violation.query.get_or_404(violation_id)

    if 'clip' not in request.files:
        return jsonify({"error": "No clip file provided"}), 400

    clip_file = request.files['clip']
    filename = secure_filename(
        f"violation_{violation_id}_{violation.event_type}.webm")
    save_path = os.path.join("static", "violation_clips", filename)

    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    clip_file.save(save_path)

    violation.clip_path = save_path
    db.session.commit()

    return jsonify({"status": "success", "clip_path": save_path})


@proctor_bp.route('/liveness/start', methods=['POST'])
def start_liveness_challenge():
    data = request.get_json()
    if not data or 'session_id' not in data:
        return jsonify({"error": "Missing session_id"}), 400
    try:
        session_id = int(data['session_id'])
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid session_id"}), 400
    challenge = _new_challenge()
    liveness_challenges[session_id] = challenge
    return jsonify({
        "status": "started",
        "challenge": challenge["type"],
        "direction": challenge["direction"],
        "instruction": _liveness_instruction(challenge["type"], challenge["direction"]),
    })


@proctor_bp.route('/liveness/frame', methods=['POST'])
def liveness_frame():
    data = request.get_json()
    if not data or 'session_id' not in data or 'image' not in data:
        return jsonify({"error": "Missing session_id or image payload"}), 400
    try:
        session_id = int(data['session_id'])
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid session_id"}), 400

    challenge = liveness_challenges.get(session_id)
    if challenge is None:
        return jsonify({"status": "not_started", "message": "Call /liveness/start first"}), 400
    if challenge["passed"]:
        return jsonify({"status": "passed"})

    try:
        image_b64 = data['image'].split(',')[1] if ',' in data['image'] else data['image']
        frame = cv2.imdecode(np.frombuffer(base64.b64decode(image_b64), np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return jsonify({"error": "Failed to decode frame"}), 400
        frame_height, frame_width = frame.shape[:2]
        num_faces, faces = detect_faces(frame)
        landmarks = faces[0] if num_faces == 1 else None

        if challenge["type"] == "blink":
            status, challenge["state"] = check_blink_challenge(
                landmarks, frame_width, frame_height, challenge["state"]
            )
        else:
            status, challenge["state"] = check_head_turn_challenge(
                landmarks, frame_width, frame_height, challenge["direction"], challenge["state"]
            )

        if status == "passed":
            challenge["passed"] = True
            return jsonify({"status": "passed"})
        if status == "failed":
            replacement = _new_challenge()
            liveness_challenges[session_id] = replacement
            return jsonify({
                "status": "failed",
                "challenge": replacement["type"],
                "direction": replacement["direction"],
                "instruction": _liveness_instruction(replacement["type"], replacement["direction"]),
            })
        if status == "no_face":
            return jsonify({"status": "no_face", "message": "Please keep your face centered in the camera"})
        return jsonify({"status": "collecting"})
    except Exception as e:
        return jsonify({"error": f"Liveness check failed: {e}"}), 500


@proctor_bp.route('/verify-identity', methods=['POST'])
def verify_identity():
    data = request.get_json()
    if not data or 'session_id' not in data or 'student_id' not in data or 'image' not in data:
        return jsonify({"error": "Missing required fields"}), 400

    session_id = int(data['session_id'])
    student_id = data['student_id']

    challenge = liveness_challenges.get(session_id)
    if challenge is None or not challenge["passed"]:
        return jsonify({"status": "liveness_required", "message": "Please complete the liveness check before verification"}), 400

    enrolled_embedding = get_or_load_signature(student_id)
    if enrolled_embedding is None:
        return jsonify({"status": "not_enrolled", "message": "No face on file for this student"}), 400

    image_b64 = data['image'].split(
        ',')[1] if ',' in data['image'] else data['image']
    nparr = np.frombuffer(base64.b64decode(image_b64), np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify({"error": "Failed to decode frame"}), 400

    num_faces, faces = detect_faces(frame)
    if num_faces != 1:
        return jsonify({"status": "retry", "message": "Position your face clearly in view"})

    distance = verify_face_deep(frame, enrolled_embedding)
    if distance is None:
        return jsonify({"status": "retry", "message": "Could not get a clear reading, try again"})

    threshold = get_deepface_threshold()
    passed = distance <= threshold

    attempt = VerificationAttempt(student_id=student_id, session_id=session_id,
                                  match_distance=distance, passed=passed)
    db.session.add(attempt)
    db.session.commit()

    return jsonify({"status": "done", "passed": passed, "distance": round(distance, 3)})
