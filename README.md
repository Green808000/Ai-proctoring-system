# AI-Powered Online Examination Proctoring System

An AI-assisted proctoring web application that watches a student's webcam during an online exam, flags suspicious behaviour, verifies the student's identity, and gives administrators a recorded, reviewable timeline of every incident.

Students register with three face photos, pass a live liveness check and identity verification, then sit a monitored exam. Administrators create exams and review each session, including video clips of the moments that were flagged.

> **Status:** academic / prototype project. It has not been hardened for production use. See [Known limitations](#known-limitations) before deploying anywhere real.

This project is a fork of [Joelokolia12/Ai-proctoring-system](https://github.com/Joelokolia12/Ai-proctoring-system) (the "V1" baseline), extended by our group.

---

## What changed from V1

| Area | V1 | V6 |
|---|---|---|
| Identity check | Geometric ratios of about 10 face landmarks | DeepFace (SFace) embeddings from three enrolled views |
| Liveness | None | Random blink or head-turn challenge before identity matching |
| Identity during the exam | Checked once | Re-verified periodically in the background |
| Video evidence | Short clip per violation | Continuous 5-second segments stitched into one clip per incident |
| Admin review | Flat list of violations | Incident timeline with a video player and jump-to-event buttons |
| Login security | None | Account lockout, explicit token expiry, dashboard idle timeout |
| Abandoned sessions | Stayed "In Progress" forever | Marked `disconnected` after 3 minutes without contact |

---

## Features

### Student side
- **Registration with three face views** (front, left, right). Each view is converted into a face embedding and stored as the student's identity reference.
- **Login** with hashed passwords, JWT session tokens, and temporary account lockout after repeated failed attempts.
- **My Exams dashboard** showing upcoming and previous exams.
- **Pre-exam verification:**
  - **Liveness challenge:** a randomly chosen blink or head-turn (left/right) that must be completed before any identity matching happens. This is meant to stop a printed photo or a photo on a phone screen.
  - **Identity verification:** the live face is compared with the enrolled embeddings using DeepFace (SFace model, cosine distance).
- **Monitored exam page:**
  - Live camera preview in a small picture-in-picture window.
  - **Lighting check** that warns when the room is too dark.
  - **Head-pose calibration** at the start, so "looking away" is measured against the student's own normal position.
  - **Continuous recording** of camera and microphone in short segments, with automatic recovery if the camera drops.
  - **Periodic identity re-verification** during the exam.

### What gets flagged

| Flag | Meaning |
|---|---|
| `ABSENCE` | No face in frame for more than 3.5 seconds |
| `MULTIPLE_FACES` | More than one face in frame for 2 seconds |
| `HEAD_MOVEMENT` | Head turned or tilted outside the calibrated range for 4 seconds |
| `IDENTITY_MISMATCH_MIDEXAM` | Periodic re-verification did not match the enrolled face |
| `IDENTITY_VERIFICATION_FAILED` | Pre-exam identity verification failed |
| `TAB_SWITCHED_OR_MINIMIZED` | The exam tab was hidden or the window minimised |

Flags are **for human review**. Nothing in the system automatically fails or blocks a student mid-exam.

### Admin side
- Separate admin registration and login.
- **Create exam:** course code, title, start time, duration, and a list of enrolled student IDs.
- **Session register:** every session grouped by day, searchable by student, ID, subject or status, with flag counts and exam time.
- **Incident review:** flagged moments are grouped into incidents. Each incident has a stitched video clip (from 10 seconds before the first event to 10 seconds after the last) and buttons that jump to each individual event in the clip.
- **Delete session:** an admin can delete a finished session, which removes its database records and stored recording files. Sessions still in progress cannot be deleted.
- **Abandoned-session handling:** if a student closes the tab or loses connection, the session is automatically marked `disconnected` after 3 minutes without contact, and any open incident clips are finalised.

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python, Flask, Flask-SQLAlchemy, Flask-JWT-Extended, Flask-SocketIO |
| Database | SQLite |
| Face landmarks | MediaPipe Face Landmarker, OpenCV |
| Identity embeddings | DeepFace (SFace model, YuNet detector) |
| Video processing | FFmpeg (required for incident clips) |
| Frontend | HTML, CSS, vanilla JavaScript (MediaRecorder for recording) |
| Auth | Werkzeug password hashing, JWT |

---

## Getting started

### Prerequisites
- **Python 3.11** (developed and tested on 3.11.6, Windows). Other versions may not work with the pinned `mediapipe` and `tensorflow` versions in `requirements.txt`.
- **FFmpeg and FFprobe** installed and available on your `PATH` (or set `FFMPEG_BIN` / `FFPROBE_BIN`). Without them, the app runs but incident video clips cannot be built.
- A webcam and microphone, and a modern Chromium-based or Firefox browser.
- Internet access on the first run: MediaPipe, DeepFace and the YuNet/SFace models download their weights the first time they are used.

### Install and run

```bash
git clone <your-repo-url>
cd Ai-proctoring-system

python -m venv venv
# Windows (PowerShell):
venv\Scripts\Activate.ps1
# macOS / Linux:
source venv/bin/activate

pip install -r requirements.txt
python app.py
```

The app is served at `http://127.0.0.1:5000`.

> `requirements.txt` is saved as UTF-16 (a side effect of creating it in PowerShell). `pip` reads it correctly, but re-saving it as UTF-8 avoids surprises with other tools.

### Set your secrets

The defaults in `config.py` are development placeholders. Set real values before using the app with anyone else:

```bash
# Windows (PowerShell)
$env:SECRET_KEY = "a-long-random-string"
$env:JWT_SECRET_KEY = "another-long-random-string"
```

### Pages

| URL | Purpose |
|---|---|
| `/` | Student login |
| `/register` | Student registration (three face photos) |
| `/my-exams` | Student dashboard |
| `/verify?exam_id=<id>` | Liveness check and identity verification |
| `/index.html?exam_id=<id>` | The monitored exam |
| `/adlogin` | Admin login |
| `/admin/register` | Admin registration |
| `/admin` | Admin dashboard |

### A typical first run
1. Register an admin at `/admin/register`, then log in at `/adlogin`.
2. Register a student at `/register`. Note the generated student ID (for example `STU-1234`).
3. As admin, create an exam and enter that student ID in the enrolled list.
4. Log in as the student at `/`, open the exam, pass the liveness and identity check, and take the exam.
5. As admin, open **Session register** and review the session.

---

## Project structure

```
Ai-proctoring-system/
├── app.py                     # Flask entry point, blueprints, page routes
├── config.py                  # Secrets, JWT expiry, lockout and heartbeat settings
├── models.py                  # SQLAlchemy models
├── session_cleanup.py         # Heartbeat + abandoned-session sweeper
├── delete_student_account.py  # Admin utility: remove a student (with DB backup)
├── routes/
│   ├── auth.py                # Register, admin register, login, lockout
│   ├── exam.py                # Exams, sessions, admin review endpoints
│   ├── proctor.py             # Frame scanning, calibration, liveness, verification
│   └── team2.py               # Recording segments, incidents, finalisation
├── detection/
│   ├── engine.py              # ProctoringEngine: runs all detectors per frame
│   ├── face_detection.py      # MediaPipe Face Landmarker wrapper
│   ├── absence_detection.py
│   ├── multiple_faces.py
│   ├── head_movement.py
│   ├── face_authentication.py # DeepFace embeddings, liveness challenges
│   └── face_landmarker.task   # MediaPipe model file
├── team2/
│   ├── store.py               # Incident / segment tables (raw SQL)
│   └── media.py               # FFmpeg clip stitching
├── templates/                 # HTML pages
├── static/
│   ├── css/  js/
│   ├── id_photos/             # Enrolment photo (front view) per student
│   ├── violation_snapshots/   # Still image per flag
│   ├── incident_segments/     # Raw 5-second recording segments
│   └── incident_clips/        # Finished incident videos
├── face_signatures/           # Per-student embeddings (.npy)
├── instance/proctoring.db     # SQLite database (created on first run)
├── scripts/                   # Fairness-testing harness (not yet used, see Future work)
└── requirements.txt
```

---

## Known limitations

- **Authentication is not yet enforced on the API.** Login issues a JWT, but no route requires it and the frontend does not send it. Admin endpoints and admin registration are reachable without logging in. This must be fixed before any real deployment.
- **Face verification is unvalidated.** The identity threshold (0.593, SFace's published default) has not been tuned or tested across lighting, skin tone, glasses or head angle. Treat identity flags as prompts for a human to look, not as proof.
- **Exam start time and duration are not enforced.** They are displayed, but a student can open any upcoming exam at any time, and nothing ends the exam when time runs out.
- **The exam content is a demo.** The 15 questions are hard-coded in `templates/index.html` and answers are not stored anywhere.
- **Camera problems are not shown to admins.** Recording failures and recoveries are logged in a database table, but the dashboard does not display them and they are not raised as flags.
- **Refreshing the exam page restarts the recording timeline.** The recording sequence and elapsed-time counters reset, which can overwrite earlier segment records for that session.
- **Browser requirements.** Camera and microphone access needs `localhost` or HTTPS.
- **Login page filename case.** `app.py` loads `Student_login.html`, but the file is named `student_login.html`. This works on Windows and macOS but fails on Linux.
- **Unused dependencies.** Flask-SocketIO is initialised in `app.py` but nothing uses it, and `requirements.txt` contains packages the app does not need (for example `google-genai` and `ipykernel`).
- **SQLite and a single process.** Suitable for development and small trials only.
- **Head-pose detection can be confused with absence** at extreme angles, because the face model may lose tracking entirely at a profile view. The landmark model tracks at most two faces.

---

## Future work

1. **Fairness and bias testing.** A test harness exists in `scripts/` (`bias_fairness_test.py`) but has **not been run**: no test photos or manifest have been collected and nothing in the application depends on it. The plan is to measure false-accept and false-reject rates across lighting, skin tone, glasses and angle, and use the results to set or adjust the identity threshold and decide how much weight identity flags should carry.
2. **Passive (continuous) liveness.** A "face has not moved for six seconds" detector is implemented but **switched off** (`PASSIVE_LIVENESS_ENABLED = False` in `detection/engine.py`). It is tuned for a faster frame rate than the exam page sends and has never triggered in testing. It needs re-tuning and validation before being enabled.
3. **Tab-switch evidence.** Capture a snapshot when a student switches tabs and record which tab or window they switched to.
4. **New exam enrolment flow.** Let admins create an exam and have students register for it, with the list of registered students and IDs sent to the admin dashboard, instead of admins typing student IDs.
5. **Enforce authentication and roles** on every API route and page.
6. **Enforce exam timing** (start window, countdown, automatic submission) and store real answers.
7. **Surface recording and camera issues** to admins.
8. **Automated tests** for the detection logic, session lifecycle and API.

---

## Housekeeping: keeping student data out of Git

The repository handles biometric and video data. Do not commit it. Make sure `.gitignore` covers at least:

```
instance/
*.db
.env
face_signatures/
static/id_photos/
static/violation_snapshots/
static/violation_clips/
static/incident_segments/
static/incident_clips/
static/recordings/
```

To remove a student and their stored face data, run `python delete_student_account.py` from the project root. It backs up the database first and keeps historical exam records.

---

## License / academic context

Built as a course / personal project. Not intended for production use without further security hardening and validation (see Known limitations).
