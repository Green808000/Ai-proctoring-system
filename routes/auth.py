import random
from datetime import datetime, timedelta
import cv2
import numpy as np
from flask import Blueprint, request, jsonify, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import create_access_token
from models import db, User
from detection.face_authentication import build_embedding, set_signature

auth_bp = Blueprint("auth", __name__)


@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.form if request.form else request.get_json() or {}

    email = data.get('email')
    password = data.get('password')
    name = data.get('name', '')

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Email is already registered"}), 400

    # New guided enrollment expects three views. Keep id_photo as a fallback
    # for older clients that still submit one reference image.
    photo_fields = ['front_photo', 'left_photo', 'right_photo']
    uploaded = {field: request.files.get(field) for field in photo_fields}

    if not all(uploaded.values()):
        legacy_photo = request.files.get('id_photo')
        if legacy_photo:
            uploaded = {'front_photo': legacy_photo, 'left_photo': None, 'right_photo': None}
        else:
            return jsonify({
                "error": "Please provide front, left, and right face photos"
            }), 400

    embeddings = []
    decoded_images = []
    for label, photo in uploaded.items():
        if photo is None:
            continue
        file_bytes = np.frombuffer(photo.read(), np.uint8)
        image_bgr = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        if image_bgr is None:
            return jsonify({"error": f"Could not read the {label.replace('_photo', '')} photo"}), 400

        try:
            embedding = build_embedding(image_bgr)
        except Exception as exc:
            current_app.logger.exception("Face enrollment failed for %s", label)
            return jsonify({"error": f"Could not process the {label.replace('_photo', '')} photo"}), 400

        if embedding is None:
            return jsonify({
                "error": f"Could not detect a clear face in the {label.replace('_photo', '')} photo"
            }), 400

        embeddings.append(np.asarray(embedding, dtype=np.float32))
        decoded_images.append((label, image_bgr))

    if len(embeddings) not in (1, 3):
        return jsonify({"error": "Provide either one legacy photo or all three face views"}), 400

    auto_student_id = f"STU-{random.randint(1000, 9999)}"
    hashed_password = generate_password_hash(password)

    import os
    os.makedirs("static/id_photos", exist_ok=True)
    id_photo_path = f"static/id_photos/{auto_student_id}.jpg"
    cv2.imwrite(id_photo_path, decoded_images[0][1])

    # A 1-D array preserves the old format; a 2-D array stores the three views.
    stored_embedding = embeddings[0] if len(embeddings) == 1 else np.vstack(embeddings)
    set_signature(auto_student_id, stored_embedding)

    new_user = User(
        name=name,
        email=email,
        password_hash=hashed_password,
        role="student",
        student_id=auto_student_id,
        id_photo_path=id_photo_path
    )
    db.session.add(new_user)
    db.session.commit()

    return jsonify({
        "status": "success",
        "message": f"Account created! Your Student ID is {auto_student_id}",
        "student_id": auto_student_id,
        "face_enrolled": True,
        "enrollment_views": len(embeddings)
    }), 201


@auth_bp.route('/admin-register', methods=['POST'])
def admin_register():
    data = request.form if request.form else request.get_json()

    email = data.get('email')
    password = data.get('password')
    name = data.get('name', '')

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Email is already registered"}), 400

    new_admin = User(
        name=name,
        email=email,
        password_hash=generate_password_hash(password),
        role="admin"
    )
    db.session.add(new_admin)
    db.session.commit()

    return jsonify({"status": "success", "message": "Admin account created"}), 201


@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.form if request.form else request.get_json()

    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    user = User.query.filter_by(email=email).first()

    # Task 5, Part A — check lockout BEFORE checking the password, so a
    # locked account can't keep being guessed against while it's locked.
    if user and user.locked_until and user.locked_until > datetime.utcnow():
        remaining_minutes = int(
            (user.locked_until - datetime.utcnow()).total_seconds() // 60
        ) + 1
        return jsonify({
            "error": "account_locked",
            "message": f"Too many failed attempts. Try again in "
                       f"{remaining_minutes} minute(s)."
        }), 429

    if not user or not check_password_hash(user.password_hash, password):
        if user:
            _register_failed_attempt(user)
        return jsonify({"error": "Invalid credentials"}), 401

    # Successful login — clear any prior failed-attempt history.
    if user.failed_login_attempts or user.locked_until:
        user.failed_login_attempts = 0
        user.locked_until = None
        db.session.commit()

    token = create_access_token(identity={
        "id": user.id,
        "role": user.role,
        "student_id": user.student_id
    })

    return jsonify({
        "status": "success",
        "token": token,
        "role": user.role,
        "student_id": user.student_id,
        "user_id": user.id,
        "name": user.name
    }), 200


def _register_failed_attempt(user):
    """Task 5, Part A. Increments the failed-attempt counter on a wrong
    password and locks the account once MAX_LOGIN_ATTEMPTS is hit.

    Note: this is account-based, not IP-based — it protects a given
    account from repeated guessing, but doesn't rate-limit an attacker
    who spreads guesses across many different accounts/emails. That's a
    known, accepted gap for now (the task brief allows either approach);
    an IP-based layer (e.g. Flask-Limiter) would be the natural follow-up
    if the team decides it's needed later.
    """
    max_attempts = current_app.config.get('MAX_LOGIN_ATTEMPTS', 5)
    lockout_minutes = current_app.config.get('LOGIN_LOCKOUT_MINUTES', 15)

    user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
    if user.failed_login_attempts >= max_attempts:
        user.locked_until = datetime.utcnow() + timedelta(minutes=lockout_minutes)
        user.failed_login_attempts = 0  # lockout is now the active control
    db.session.commit()
