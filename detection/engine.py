import time
from statistics import median

from .face_detection import detect_faces
from .absence_detection import check_absence
from .multiple_faces import check_multiple_faces
from .head_movement import check_head_movement, calibrate_head_pose
from .face_authentication import (
    check_sustained_liveness,
    get_or_load_signature,
    get_deepface_threshold,
    verify_face_deep,
)

CALIBRATION_DURATION = 4

# Passive liveness (flags a face that stays almost motionless for 6+ seconds).
# Switched off: it has never fired in testing, and it is tuned for a faster
# frame rate than the exam page sends (one frame every 3 seconds), so it is
# unlikely to be meaningful as configured. The interactive blink / head-turn
# check on the verify page is separate and is NOT affected by this flag.
# Set to True to re-enable it.
PASSIVE_LIVENESS_ENABLED = False
MID_EXAM_REVERIFICATION_INTERVAL = 30
MID_EXAM_REVERIFICATION_WINDOW = 10
MID_EXAM_MIN_SAMPLES = 3


class ProctoringEngine:
    def __init__(self, student_id=None, enrolled_embedding=None):
        self.absence_start_time = None
        self.absence_next_report_time = None
        self.absence_incident_id = None

        self.multi_face_start_time = None
        self.multi_face_flagged = False
        self.multi_face_incident_id = None

        self.head_pose_baseline = None
        self.head_deviation_start_time = None
        self.head_flagged = False
        self.head_incident_id = None

        self.student_id = student_id
        self.enrolled_embedding = (
            enrolled_embedding if enrolled_embedding is not None
            else (get_or_load_signature(student_id) if student_id else None)
        )
        self.next_identity_check_time = None
        self.last_identity_check = None
        self.identity_window_started_at = None
        self.identity_window_distances = []

        self.liveness_recent_bboxes = []
        self.liveness_static_since = None
        self.liveness_last_event_time = None
        self.liveness_incident_id = None

        self._calibrating = False
        self._calibration_start_time = None
        self._calibration_frames = []
        self.event_history = []

    def set_identity(self, student_id):
        self.student_id = student_id
        self.enrolled_embedding = get_or_load_signature(
            student_id) if student_id else None
        self.next_identity_check_time = None
        self.last_identity_check = None
        self.identity_window_started_at = None
        self.identity_window_distances = []

    def _check_mid_exam_identity(self, frame, now):
        """Verify identity using several frames collected over a short window.

        A single frame can be unreliable while the student turns their head.
        We therefore collect valid distances for up to 10 seconds and use the
        median distance for the final decision. This is only for mid-exam
        re-verification; login verification is unchanged.
        """
        self.last_identity_check = None
        if self.student_id is None or self.enrolled_embedding is None:
            return None

        # Schedule the next verification window.
        if self.identity_window_started_at is None:
            if self.next_identity_check_time is None:
                self.next_identity_check_time = now + MID_EXAM_REVERIFICATION_INTERVAL
                return None
            if now < self.next_identity_check_time:
                return None
            self.identity_window_started_at = now
            self.identity_window_distances = []

        # Collect one sample from the current incoming frame.
        distance = verify_face_deep(frame, self.enrolled_embedding)
        if distance is not None:
            self.identity_window_distances.append(float(distance))

        window_elapsed = now - self.identity_window_started_at
        if window_elapsed < MID_EXAM_REVERIFICATION_WINDOW:
            return None

        distances = self.identity_window_distances[:]
        self.identity_window_started_at = None
        self.identity_window_distances = []
        self.next_identity_check_time = now + MID_EXAM_REVERIFICATION_INTERVAL

        threshold = get_deepface_threshold()
        enough_samples = len(distances) >= MID_EXAM_MIN_SAMPLES
        representative_distance = median(distances) if distances else None
        passed = enough_samples and representative_distance <= threshold

        self.last_identity_check = {
            "performed": True,
            "passed": passed,
            "timestamp": now,
            "samples": len(distances),
            "window_seconds": MID_EXAM_REVERIFICATION_WINDOW,
            "distance": round(representative_distance, 3) if representative_distance is not None else None,
            "threshold": threshold,
        }

        if passed:
            return None
        return {
            "event_type": "IDENTITY_MISMATCH_MIDEXAM",
            "severity": "high",
            "distance": round(representative_distance, 3) if representative_distance is not None else None,
            "samples": len(distances),
            "timestamp": now,
        }

    def start_calibration(self):
        self._calibrating = True
        self._calibration_start_time = time.time()
        self._calibration_frames = []

    def process_frame(self, frame):
        now = time.time()
        num_faces, faces = detect_faces(frame)
        landmarks = faces[0].landmark if faces else None
        frame_height, frame_width = frame.shape[:2]
        calibration_done_this_frame = False

        if self._calibrating:
            self._calibration_frames.append(landmarks)
            if now - self._calibration_start_time >= CALIBRATION_DURATION:
                self.head_pose_baseline = calibrate_head_pose(
                    self._calibration_frames, frame_width, frame_height
                )
                self._calibrating = False
                calibration_done_this_frame = True

        absence_event, self.absence_start_time, self.absence_next_report_time, self.absence_incident_id = check_absence(
            num_faces, self.absence_start_time, self.absence_next_report_time, self.absence_incident_id
        )
        multi_event, self.multi_face_start_time, self.multi_face_flagged, self.multi_face_incident_id = check_multiple_faces(
            num_faces, self.multi_face_start_time, self.multi_face_flagged, self.multi_face_incident_id
        )

        head_event = None
        if not self._calibrating and self.head_pose_baseline is not None:
            head_event, self.head_deviation_start_time, self.head_flagged, self.head_incident_id = check_head_movement(
                landmarks, self.head_pose_baseline, frame_width, frame_height,
                self.head_deviation_start_time, self.head_flagged, self.head_incident_id
            )

        identity_event = self._check_mid_exam_identity(frame, now)

        liveness_event = None
        if PASSIVE_LIVENESS_ENABLED and not self._calibrating and faces:
            liveness_event, self.liveness_recent_bboxes, self.liveness_static_since, self.liveness_last_event_time, self.liveness_incident_id = check_sustained_liveness(
                faces[0], frame_width, frame_height, self.liveness_recent_bboxes,
                self.liveness_static_since, self.liveness_last_event_time,
                self.liveness_incident_id, now=now
            )

        events = []
        for event in (absence_event, multi_event, head_event, identity_event, liveness_event):
            if event:
                events.append(event)
                self.event_history.append(event)

        return {
            "face_count": num_faces,
            "faces": faces,
            "events": events,
            "calibrating": self._calibrating,
            "calibration_done": calibration_done_this_frame,
            "baseline_ready": self.head_pose_baseline is not None,
            "identity_check": self.last_identity_check,
        }

    def get_event_history(self):
        return self.event_history
