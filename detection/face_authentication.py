"""
Face authentication for the AI proctoring system.

Checks whether the face currently in frame matches a face enrolled earlier
(either live, at the start of a session, or from a pre-existing reference
photo). Like every other detector here, this does NOT auto-fail a session
a sustained mismatch produces an event for a human reviewer, the same
way absence/multiple-faces/head-movement do.

Uses ONLY the Face Mesh landmarks face_detection.py already computes for
every frame no second model, no OpenCV recognizer, nothing beyond
MediaPipe and plain arithmetic. The identity "signature" is a set of
distance RATIOS between ten landmark points (eyes, mouth corners, jaw,
chin, forehead), each divided by the outer eye-to-eye distance so the
signature is invariant to how close someone sits to the camera -- verified
directly: the same synthetic face at very different scale/position produces
an exact-zero distance in this representation.

Be clear-eyed about what this buys you. This is the WEAKEST of the three
approaches looked at for this feature (a trained deep face-recognition
embedding like ArcFace > OpenCV's LBPH pixel-texture matching > this).
MediaPipe's landmarks were trained for pose and expression tracking, not
for telling people apart geometric ratios between ~10 points carry much
less identity information than either pixel texture or a learned embedding.
It's a real, pre-deep-learning face-recognition technique, not something
invented for this project, but treat it as "catches an obvious swap," not
"confidently verifies identity." Two mitigations already built in:
distances use ratios (pose/scale robust) rather than raw pixel gaps, and
the sustained-mismatch window in check_identity() means a few seconds of
talking or turning your head won't misfire only a persistent mismatch
does. If accuracy proves too weak once tested against real faces,
verify_face() and the two enroll_*() functions are the only things that
need to change to swap in a stronger approach later -- check_identity()
doesn't care how the distance was computed.

NOTE: this file now also includes a DeepFace-based replacement path
(build_embedding / verify_face_deep / get_deepface_threshold) -- see the
"DeepFace-based verification" section near the bottom. That is the path
actually wired up in routes/auth.py and routes/proctor.py going forward;
the geometric-ratio functions above are kept as a reference/fallback.

Also includes an optional thread-safe, per-student, disk-backed session
store (get_or_load_signature / set_signature / clear_signature) for cases
where enrollment needs to happen independent of a live session, or where
this ends up shared across threads rather than one instance per session --
and a landmark-based liveness heuristic (check_liveness), which is
supplemental only and explicitly NOT a security control on its own.

VERIFICATION STATUS (see ENGINE_INTEGRATION_GUIDE.md header for the fuller
project history this is part of): crop/ratio math and the check_identity
state machine (sustained-mismatch timing, re-fire cadence, reset-on-match,
reset-on-no-landmarks) were tested directly with mocked landmarks and a
controlled time sequence -- both behave exactly as designed. What was NOT
tested: real-world discrimination accuracy. That requires real faces and a
webcam, neither of which were available while building this. Treat
MISMATCH_DISTANCE_THRESHOLD and LIVENESS_MIN_MOVEMENT as starting points,
not tuned values, until you've run this against real enrollment/verification
pairs.
"""

import threading
import time
import uuid
from pathlib import Path

import numpy as np

from .face_detection import detect_faces


# ============================================================================
# Landmark indices
# ============================================================================
EYE_L_OUTER, EYE_L_INNER = 33, 133
EYE_R_INNER, EYE_R_OUTER = 362, 263
MOUTH_L, MOUTH_R = 61, 291
CHIN, FOREHEAD = 152, 10
JAW_L, JAW_R = 234, 454


# ============================================================================
# Tuning constants
# ============================================================================
MISMATCH_DISTANCE_THRESHOLD = 0.25
MISMATCH_SUSTAINED_SECONDS = 5.0
MISMATCH_REFIRE_SECONDS = 10.0
MIN_ENROLLMENT_SAMPLES = 5

LIVENESS_BUFFER_SIZE = 8
LIVENESS_MIN_MOVEMENT = 0.002

SIGNATURE_DIR = "face_signatures"


# ============================================================================
# Geometric signature
# ============================================================================

def _landmark_xy(landmarks, index, frame_width, frame_height):
    lm = landmarks.landmark[index]
    return np.array([lm.x * frame_width, lm.y * frame_height])


def _feature_vector(landmarks, frame_width, frame_height):
    def p(i): return _landmark_xy(landmarks, i, frame_width, frame_height)
    def d(a, b): return float(np.linalg.norm(a - b))

    eye_L_outer, eye_L_inner = p(EYE_L_OUTER), p(EYE_L_INNER)
    eye_R_inner, eye_R_outer = p(EYE_R_INNER), p(EYE_R_OUTER)
    mouth_L, mouth_R = p(MOUTH_L), p(MOUTH_R)
    chin, forehead = p(CHIN), p(FOREHEAD)
    jaw_L, jaw_R = p(JAW_L), p(JAW_R)

    ref = d(eye_L_outer, eye_R_outer)
    if ref < 1e-6:
        return None

    return np.array([
        d(eye_L_inner, eye_R_inner) / ref,
        d(jaw_L, jaw_R) / ref,
        d(mouth_L, mouth_R) / ref,
        d(forehead, chin) / ref,
        d(eye_L_outer, jaw_L) / ref,
        d(eye_R_outer, jaw_R) / ref,
        d(eye_L_outer, mouth_L) / ref,
        d(eye_R_outer, mouth_R) / ref,
        (d(mouth_L, chin) + d(mouth_R, chin)) / (2 * ref),
    ])


def enroll_from_frames(frame_landmark_pairs, frame_width, frame_height):
    vectors = []
    for _frame, landmarks in frame_landmark_pairs:
        if landmarks is None:
            continue
        fv = _feature_vector(landmarks, frame_width, frame_height)
        if fv is not None:
            vectors.append(fv)

    if len(vectors) < MIN_ENROLLMENT_SAMPLES:
        return None

    return np.median(np.stack(vectors), axis=0)


def enroll_from_image(image_bgr):
    frame_height, frame_width = image_bgr.shape[:2]
    num_faces, faces = detect_faces(image_bgr)
    if num_faces == 0:
        return None
    return _feature_vector(faces[0], frame_width, frame_height)


def verify_face(landmarks, enrolled_signature, frame_width, frame_height):
    fv = _feature_vector(landmarks, frame_width, frame_height)
    if fv is None:
        return None
    return float(np.linalg.norm(fv - enrolled_signature))


def check_identity(landmarks, enrolled_signature, frame_width, frame_height,
                   mismatch_since, last_event_time, incident_id, now=None):
    now = now if now is not None else time.time()

    if landmarks is None or enrolled_signature is None:
        return None, None, None, None

    distance = verify_face(landmarks, enrolled_signature,
                           frame_width, frame_height)
    if distance is None:
        return None, None, None, None

    if distance <= MISMATCH_DISTANCE_THRESHOLD:
        return None, None, None, None

    if mismatch_since is None:
        mismatch_since = now

    elapsed = now - mismatch_since
    if elapsed < MISMATCH_SUSTAINED_SECONDS:
        return None, mismatch_since, last_event_time, incident_id

    if incident_id is None:
        incident_id = f"IDN-{uuid.uuid4().hex[:8]}"

    if last_event_time is None or (now - last_event_time) >= MISMATCH_REFIRE_SECONDS:
        event = {
            "incident_id": incident_id,
            "event_type": "identity_mismatch",
            "timestamp": now,
            "duration": round(elapsed, 1),
            "distance": round(distance, 3),
        }
        return event, mismatch_since, now, incident_id

    return None, mismatch_since, last_event_time, incident_id


# ============================================================================
# Liveness heuristic (supplemental only)
# ============================================================================

def _face_bbox_from_landmarks(landmarks, frame_width, frame_height):
    xs = [lm.x * frame_width for lm in landmarks.landmark]
    ys = [lm.y * frame_height for lm in landmarks.landmark]
    return np.array([min(xs), min(ys), max(xs), max(ys)])


def check_liveness(landmarks, frame_width, frame_height, recent_bboxes):
    if landmarks is None:
        return "insufficient_data", recent_bboxes

    bbox = _face_bbox_from_landmarks(landmarks, frame_width, frame_height)
    recent_bboxes = (recent_bboxes + [bbox])[-LIVENESS_BUFFER_SIZE:]

    if len(recent_bboxes) < LIVENESS_BUFFER_SIZE:
        return "insufficient_data", recent_bboxes

    box_w = max(bbox[2] - bbox[0], 1.0)
    box_h = max(bbox[3] - bbox[1], 1.0)
    diag = float(np.hypot(box_w, box_h))

    deltas = [
        float(np.linalg.norm(recent_bboxes[i] - recent_bboxes[i - 1])) / diag
        for i in range(1, len(recent_bboxes))
    ]
    avg_movement = sum(deltas) / len(deltas)
    status = "moving" if avg_movement >= LIVENESS_MIN_MOVEMENT else "static"
    return status, recent_bboxes


# ============================================================================
# Sustained liveness monitoring
# ============================================================================

LIVENESS_STATIC_SUSTAINED_SECONDS = 6.0
LIVENESS_STATIC_REFIRE_SECONDS = 15.0


def check_sustained_liveness(
    landmarks,
    frame_width,
    frame_height,
    recent_bboxes,
    static_since,
    last_event_time,
    incident_id,
    now=None,
):
    """
    Detects whether a face remains almost completely motionless
    for a sustained period during the examination.

    This is a supplemental liveness signal, not an automatic
    examination failure.
    """

    now = now if now is not None else time.time()

    status, recent_bboxes = check_liveness(
        landmarks,
        frame_width,
        frame_height,
        recent_bboxes,
    )

    # Reset the sustained-static timer when movement is detected
    if status != "static":
        return (
            None,
            recent_bboxes,
            None,
            None,
            None,
        )

    # Start timing when the face becomes static
    if static_since is None:
        static_since = now

    elapsed = now - static_since

    # Wait until the face has remained static long enough
    if elapsed < LIVENESS_STATIC_SUSTAINED_SECONDS:
        return (
            None,
            recent_bboxes,
            static_since,
            last_event_time,
            incident_id,
        )

    # Create an incident ID if one does not already exist
    if incident_id is None:
        incident_id = f"LIV-{uuid.uuid4().hex[:8]}"

    # Prevent repeated events from being generated too frequently
    if (
        last_event_time is None
        or (now - last_event_time) >= LIVENESS_STATIC_REFIRE_SECONDS
    ):
        event = {
            "incident_id": incident_id,
            "event_type": "liveness_suspicious",
            "timestamp": now,
            "duration": round(elapsed, 1),
        }

        return (
            event,
            recent_bboxes,
            static_since,
            now,
            incident_id,
        )

    return (
        None,
        recent_bboxes,
        static_since,
        last_event_time,
        incident_id,
    )


# ============================================================================
# Persistence
# ============================================================================

def save_enrollment(signature, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, signature)


def load_enrollment(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        return np.load(path)
    except (OSError, ValueError):
        try:
            path.rename(path.with_suffix(".npy.corrupted"))
        except OSError:
            pass
        return None


# ============================================================================
# Multi-student session store (optional)
# ============================================================================

_sessions_lock = threading.RLock()
_signatures = {}


def _signature_path(student_id):
    path = Path(SIGNATURE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{student_id}.npy"


def get_or_load_signature(student_id):
    with _sessions_lock:
        if student_id in _signatures:
            return _signatures[student_id]

    signature = load_enrollment(_signature_path(student_id))
    if signature is not None:
        with _sessions_lock:
            _signatures[student_id] = signature
    return signature


def set_signature(student_id, signature, persist=True):
    with _sessions_lock:
        _signatures[student_id] = signature
    if persist:
        save_enrollment(signature, _signature_path(student_id))


def clear_signature(student_id, delete_persisted=True):
    with _sessions_lock:
        _signatures.pop(student_id, None)
    if delete_persisted:
        path = _signature_path(student_id)
        if path.exists():
            path.unlink()


def list_enrolled_students():
    path = Path(SIGNATURE_DIR)
    if not path.exists():
        return []
    return [f.stem for f in path.glob("*.npy") if f.is_file()]


# ============================================================================
# DeepFace-based verification (stronger replacement for the ratio approach)
# ============================================================================
DEEPFACE_MODEL = "SFace"
DEEPFACE_DISTANCE_METRIC = "cosine"

# SFace's own published cosine-distance threshold, from DeepFace's source.
# Using a fixed value here instead of importing it from inside DeepFace,
# since that internal module path has moved between DeepFace versions.
# Starting point only — needs real-world tuning (see Task 6).
DEEPFACE_MISMATCH_THRESHOLD = 0.593


def build_embedding(image_bgr):
    """
    Compute a face embedding using a trained recognition model (DeepFace),
    replacing the geometric-ratio signature above.
    Returns a 1-D numpy array, or None if no face was found.
    """
    from deepface import DeepFace  # imported lazily so this module still
    # loads even before deepface is installed
    try:
        reps = DeepFace.represent(
            img_path=image_bgr,
            model_name=DEEPFACE_MODEL,
            detector_backend="yunet",  # OpenCV-native detector (no
            # TensorFlow/Keras involved at all);
            # "mediapipe" and "retinaface" were
            # tried first but both hit
            # TensorFlow/Keras-3 compatibility
            # bugs against this project's very
            # new tensorflow==2.21.0
            enforce_detection=True,
        )
    except ValueError as e:
        # TEMP: remove once stable
        print(f"[build_embedding] face detection failed: {e}")
        return None
    if not reps:
        return None
    return np.array(reps[0]["embedding"])


def _cosine_distance(a, b):
    a = a / np.linalg.norm(a)
    b = b / np.linalg.norm(b)
    return float(1 - np.dot(a, b))


def get_deepface_threshold():
    """Returns the mismatch threshold to compare a cosine distance against.
    See DEEPFACE_MISMATCH_THRESHOLD above for why this is a fixed constant
    rather than pulled from inside DeepFace."""
    return DEEPFACE_MISMATCH_THRESHOLD


def verify_face_deep(frame_bgr, enrolled_embedding):
    """Return the closest cosine distance against one or more embeddings.

    Older registrations contain one 1-D embedding. New registrations may
    contain a 2-D array with center/left/right embeddings.
    """
    live_embedding = build_embedding(frame_bgr)
    if live_embedding is None or enrolled_embedding is None:
        return None

    enrolled = np.asarray(enrolled_embedding)
    if enrolled.ndim == 1:
        return _cosine_distance(live_embedding, enrolled)

    if enrolled.ndim != 2 or len(enrolled) == 0:
        return None

    distances = [_cosine_distance(live_embedding, reference)
                 for reference in enrolled]
    return min(distances) if distances else None

# ============================================================================
# Interactive liveness challenges
# ============================================================================

# MediaPipe Face Mesh landmark indices used for a simple blink estimate.
_BLINK_LEFT = (33, 160, 158, 133, 153, 144)
_BLINK_RIGHT = (362, 385, 387, 263, 373, 380)


def _point_xy(landmarks, index, frame_width, frame_height):
    lm = landmarks.landmark[index]
    return np.array([lm.x * frame_width, lm.y * frame_height], dtype=float)


def _eye_aspect_ratio(landmarks, indices, frame_width, frame_height):
    p1, p2, p3, p4, p5, p6 = [
        _point_xy(landmarks, i, frame_width, frame_height) for i in indices
    ]
    horizontal = np.linalg.norm(p1 - p4)
    if horizontal <= 1e-6:
        return 0.0
    vertical = (np.linalg.norm(p2 - p6) + np.linalg.norm(p3 - p5)) / 2.0
    return float(vertical / horizontal)


def _blink_ratio(landmarks, frame_width, frame_height):
    left = _eye_aspect_ratio(landmarks, _BLINK_LEFT, frame_width, frame_height)
    right = _eye_aspect_ratio(landmarks, _BLINK_RIGHT, frame_width, frame_height)
    return (left + right) / 2.0


def new_blink_challenge_state():
    return {
        "started_at": time.time(),
        "closed_seen": False,
        "frames": 0,
    }


def check_blink_challenge(landmarks, frame_width, frame_height, state):
    """Return (status, updated_state) for a blink challenge."""
    state = state or new_blink_challenge_state()
    now = time.time()

    if landmarks is None:
        return "no_face", state

    state["frames"] += 1
    elapsed = now - state["started_at"]
    if elapsed > 10.0:
        return "failed", new_blink_challenge_state()

    ratio = _blink_ratio(landmarks, frame_width, frame_height)

    # These are starting values and should be tuned with webcam testing.
    if ratio < 0.18:
        state["closed_seen"] = True
        return "collecting", state

    if state["closed_seen"] and ratio >= 0.20:
        return "passed", state

    return "collecting", state


def new_head_turn_challenge_state():
    return {
        "started_at": time.time(),
        "baseline_yaws": [],
        "baseline_yaw": None,
        "turned_seen": False,
    }


def check_head_turn_challenge(
    landmarks,
    frame_width,
    frame_height,
    direction,
    state,
):
    """Return (status, updated_state) for a left/right head-turn challenge."""
    state = state or new_head_turn_challenge_state()
    now = time.time()

    if landmarks is None:
        return "no_face", state

    if now - state["started_at"] > 10.0:
        return "failed", new_head_turn_challenge_state()

    # Import here to avoid creating a module-level dependency cycle.
    from .head_movement import get_pose_ratios

    yaw, _pitch = get_pose_ratios(landmarks.landmark, frame_width, frame_height)

    if state["baseline_yaw"] is None:
        state["baseline_yaws"].append(yaw)
        if len(state["baseline_yaws"]) >= 5:
            state["baseline_yaw"] = float(np.median(state["baseline_yaws"]))
        return "collecting", state

    delta = yaw - state["baseline_yaw"]
    required_delta = 18.0 if direction == "left" else -18.0

    if (direction == "left" and delta >= required_delta) or (
        direction == "right" and delta <= required_delta
    ):
        state["turned_seen"] = True
        return "passed", state

    return "collecting", state
