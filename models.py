from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(50), unique=True, nullable=True)
    name = db.Column(db.String(100), nullable=True) # Changed to nullable=True
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False) # Increased length to 255
    role = db.Column(db.String(20), default="student")
    id_photo_path = db.Column(db.String(255), nullable=True)
    face_embedding = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- Task 5, Part A: login rate-limiting ---
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)

class Exam(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    course_code = db.Column(db.String(20), nullable=False)
    course_title = db.Column(db.String(150), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    time_hours = db.Column(db.Float, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))

class ExamEnrollment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id"), nullable=False)
    student_id = db.Column(db.String(50), nullable=False)

class ExamSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id"), nullable=False)
    student_id = db.Column(db.String(50), nullable=False)
    start_time = db.Column(db.DateTime, default=datetime.utcnow)
    end_time = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default="In Progress")
    video_path = db.Column(db.String(255), nullable=True)
    audio_path = db.Column(db.String(255), nullable=True)
    # Last time the student's page contacted the server. Used by
    # session_cleanup.py to detect abandoned sessions (tab closed, crash).
    last_heartbeat = db.Column(db.DateTime, nullable=True)

class VerificationAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(50), nullable=False)
    session_id = db.Column(db.Integer, db.ForeignKey('exam_session.id'), nullable=True)
    match_distance = db.Column(db.Float, nullable=False)
    passed = db.Column(db.Boolean, nullable=False)
    live_snapshot_path = db.Column(db.String(255), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Violation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('exam_session.id'), nullable=False)
    event_type = db.Column(db.String(50), nullable=False)
    severity = db.Column(db.String(10), default='medium')
    confidence = db.Column(db.Float, nullable=True)
    snapshot_path = db.Column(db.String(255), nullable=True)
    clip_path = db.Column(db.String(255), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)