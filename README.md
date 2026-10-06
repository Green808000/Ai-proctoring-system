# AI-Powered Proctoring and Timed Assessment System 2.0

An AI-assisted web application that watches a student's webcam during an online exam, flags suspicious behaviour, verifies the student's identity, and gives administrators a recorded, reviewable timeline of every incident.

Students register with three face photos, pass a live liveness check and identity verification, then sit a monitored exam. Administrators create exams and review each session, including video clips of the flagged moments.

> **Status:** academic prototype. It has not been hardened for production. Read [Known limitations](#known-limitations) before deploying anywhere real.

**Course context:** Built by **Group 1** as a project for the course at [**NCAIR**](https://ncair.nitda.gov.ng/) under the guidance of our facilitators and project supervisors, **Victor Rizama** and **Stephen Ayuba**. See [Acknowledgements](#acknowledgements).

This project is a fork and extension of [Joelokolia12/Ai-proctoring-system](https://github.com/Joelokolia12/Ai-proctoring-system) (the "V1" baseline).

---

## Table of contents

1. [Main idea](#main-idea)
2. [Methodology](#methodology)
3. [What changed from V1](#what-changed-from-v1)
4. [Features](#features)
5. [Tech stack](#tech-stack)
6. [Project structure](#project-structure)
7. [Data, models and files used](#data-models-and-files-used)
8. [Getting started](#getting-started)
9. [Configuration](#configuration)
10. [Usage walkthrough](#usage-walkthrough)
11. [Evaluation so far](#evaluation-so-far)
12. [Known limitations](#known-limitations)
13. [Future work](#future-work)
14. [Slides and report](#slides-and-report)
15. [Protecting student data](#protecting-student-data)
16. [Team](#team)
17. [Acknowledgements](#acknowledgements)
18. [License](#license)

---

## Main idea

Remote exams raise two questions: **is the right person taking the exam**, and **did they behave normally throughout**? Human invigilators cannot watch every webcam, and the V1 baseline only checked identity once, with a weak face signature, and kept little usable evidence.

Our goal in V6 is a proctoring system that:

1. Confirms the student is a **live person** (not a photo) before the exam starts.
2. Confirms the student is **the enrolled person**, using learned face embeddings.
3. **Re-checks identity during the exam**, not only at the start.
4. **Flags** absence, extra people, head turns and tab switching.
5. Keeps **reviewable video evidence** so a human makes the final decision.

Flags are prompts for human review. Nothing in the system automatically fails or blocks a student.

---

## Methodology

The system runs a pipeline from enrolment to review.

**1. Enrolment.** The student submits three photos (front, left, right). Each photo is decoded with OpenCV, the face is found with the YuNet detector, and the **SFace** model converts it into a 128-number embedding. The three embeddings are stacked into a 3x128 array and saved as `face_signatures/<student_id>.npy`. The front photo is also saved to `static/id_photos/`.

**2. Pre-exam liveness.** A random challenge (blink, turn left or turn right) must be completed first, using MediaPipe Face Landmarker landmarks. This is meant to defeat printed photos and photos on a phone screen.

**3. Pre-exam identity verification.** One live frame is embedded and compared with the three stored embeddings using **cosine distance** (1 minus the dot product of the normalised vectors). The smallest distance is used. The student passes if it is at or below **0.593**, SFace's published default threshold. Every attempt is logged in the `VerificationAttempt` table.

**4. Low-light handling (CLAHE).** If the face image is dark (mean lightness below 130), Contrast Limited Adaptive Histogram Equalisation is applied to the lightness channel only (LAB colour space, clip limit 2.0, 8x8 tiles) before the embedding is computed. See [Evaluation so far](#evaluation-so-far) for what has and has not been proven.

**5. Continuous monitoring.** The browser sends a frame about every 3 seconds. MediaPipe landmarks drive four detectors: face absence (3.5 s), multiple faces (2 s), head movement against a per-student calibrated pose (4 s), and tab switching (browser visibility events).

**6. Mid-exam re-verification.** Every 30 seconds a 10-second window of frames is embedded and the median distance is compared with the threshold. At least 3 usable samples are required.

**7. Evidence.** The browser records camera and microphone in 5-second segments. Flagged events are grouped into incidents. FFmpeg stitches the segments into one clip per incident, from 10 seconds before the first event to 10 seconds after the last. Optional captions are generated locally with faster-whisper.

**8. Review.** Administrators open the session register, play the incident clip, and use jump-to-event buttons to inspect each flag.

**Session housekeeping.** A background daemon thread (`session_cleanup.py`) runs every 30 seconds, marks sessions `disconnected` after 3 minutes without contact, and finalises open incident clips.

---

## What changed from V1

| Area | V1 | V6 |
|---|---|---|
| Identity check | Geometric ratios of about 10 face landmarks | DeepFace (SFace) embeddings from three enrolled views |
| Low-light handling | None | CLAHE on dark faces before embedding |
| Liveness | None | Random blink or head-turn challenge before identity matching |
| Identity during the exam | Checked once | Re-verified periodically in the background |
| Video evidence | Short clip per violation | Continuous 5-second segments stitched into one clip per incident |
| Admin review | Flat list of violations | Incident timeline with a video player and jump-to-event buttons |
| Session register | Flat list | Grouped by student (collapsible) or by day, searchable |
| Clip captions | None | Optional local subtitles for incident clips |
| Login security | None | Account lockout, explicit token expiry, dashboard idle timeout |
| Abandoned sessions | Stayed "In Progress" forever | Marked `disconnected` after 3 minutes without contact |

---

## Features

### Student side
- **Registration with three face views** (front, left, right), each stored as an embedding.
- **Login** with hashed passwords, JWT tokens, and temporary lockout after repeated failed attempts (5 failures, 30-second lock).
- **My Exams dashboard** showing upcoming and previous exams.
- **Pre-exam verification:** liveness challenge, then identity verification.
- **Monitored exam page:** picture-in-picture camera preview, lighting warning, head-pose calibration, continuous segment recording with automatic camera recovery, and periodic identity re-verification.

### What gets flagged

| Flag | Meaning |
|---|---|
| `ABSENCE` | No face in frame for more than 3.5 seconds |
| `MULTIPLE_FACES` | More than one face in frame for 2 seconds |
| `HEAD_MOVEMENT` | Head turned or tilted outside the calibrated range for 4 seconds |
| `IDENTITY_MISMATCH_MIDEXAM` | Periodic re-verification did not match the enrolled face |
| `IDENTITY_VERIFICATION_FAILED` | Pre-exam identity verification failed |
| `TAB_SWITCHED_OR_MINIMIZED` | The exam tab was hidden or the window minimised |

### Admin side
- Separate admin registration and login.
- **Create exam:** course code, title, start time, duration, and enrolled student IDs.
- **Session register:** grouped by student or by day, searchable by student, ID, subject or status, with flag counts and exam time.
- **Incident review:** stitched video clip, jump-to-event buttons, and optional captions.
- **Delete session:** removes a finished session's database records and recording files. Sessions in progress cannot be deleted.
- **Abandoned-session handling:** automatic `disconnected` status and clip finalisation.

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python, Flask, Flask-SQLAlchemy, Flask-JWT-Extended, Flask-SocketIO |
| Database | SQLite |
| Face landmarks, liveness, head pose | MediaPipe Face Landmarker, OpenCV |
| Identity embeddings | DeepFace (SFace model, YuNet detector), NumPy |
| Low-light enhancement | OpenCV CLAHE |
| Video processing | FFmpeg and FFprobe |
| Captions (optional) | faster-whisper (local, offline) |
| Frontend | HTML, CSS, vanilla JavaScript (MediaRecorder for recording) |
| Auth | Werkzeug password hashing, JWT |

**Why YuNet as the detector?** MediaPipe and RetinaFace backends hit TensorFlow/Keras 3 compatibility errors inside DeepFace in our environment, so YuNet was used.

---

## Project structure

```
Ai-proctoring-system/
├── app.py                     # Flask entry point, blueprints, page routes
├── config.py                  # Secrets, JWT expiry, lockout and heartbeat settings
├── models.py                  # SQLAlchemy models
├── session_cleanup.py         # Heartbeat + abandoned-session sweeper (background thread)
├── delete_student_account.py  # Admin utility: remove a student (with DB backup)
├── requirements.txt           # Python dependencies
├── routes/
│   ├── auth.py                # Register, admin register, login, lockout
│   ├── exam.py                # Exams, sessions, admin review endpoints
│   ├── proctor.py             # Frame scanning, calibration, liveness, verification
│   └── team2.py               # Recording segments, incidents, captions, finalisation
├── detection/
│   ├── engine.py              # ProctoringEngine: runs all detectors per frame
│   ├── face_detection.py      # MediaPipe Face Landmarker wrapper
│   ├── absence_detection.py
│   ├── multiple_faces.py
│   ├── head_movement.py
│   ├── face_authentication.py # DeepFace embeddings, CLAHE, liveness challenges
│   └── face_landmarker.task   # MediaPipe model file
├── team2/
│   ├── store.py               # Incident / segment tables (raw SQL)
│   ├── media.py               # FFmpeg clip stitching
│   └── captions.py            # Local subtitle generation (faster-whisper)
├── templates/                 # HTML pages
├── static/
│   ├── css/  js/
│   ├── id_photos/             # Enrolment photo (front view) per student  [generated]
│   ├── violation_snapshots/   # Still image per flag                      [generated]
│   ├── incident_segments/     # Raw 5-second recording segments           [generated]
│   └── incident_clips/        # Finished incident videos                  [generated]
├── face_signatures/           # Per-student embeddings (.npy)             [generated]
├── instance/proctoring.db     # SQLite database                           [generated]
├── scripts/
│   ├── bias_fairness_test.py  # Fairness-testing harness
│   └── README_fairness_testing.md
├── docs/                      # Slides and project report (see below)
└── README.md
```

Folders marked `[generated]` are created at runtime and are not in the repository.

---

## Data, models and files used

The project uses **no external dataset**. All data is created by running the system.

| What | Where it comes from | Where it lives |
|---|---|---|
| Face embeddings (3x128 float32 per student) | Created at registration by SFace | `face_signatures/<student_id>.npy` |
| Front-view ID photo | Uploaded at registration | `static/id_photos/<student_id>.jpg` |
| Users, exams, sessions, flags, verification attempts | Created by using the app | `instance/proctoring.db` (SQLite) |
| Recording segments and incident clips | Recorded in the browser during an exam, stitched by FFmpeg | `static/incident_segments/`, `static/incident_clips/` |
| MediaPipe Face Landmarker model | Included in the repo | `detection/face_landmarker.task` |
| SFace and YuNet weights | Downloaded automatically by DeepFace on first use | DeepFace's local weights folder |
| Whisper model (captions only) | Downloaded by faster-whisper on first use | faster-whisper's local cache |

**To reproduce the identity results**, register one or more students with your own webcam, then run pre-exam verification repeatedly. Each attempt (distance and pass/fail) is stored in the `verification_attempt` table.

**To run the fairness harness**, prepare a CSV manifest with these columns, then follow `scripts/README_fairness_testing.md`:

```
pair_id, enrolled_image, test_image, expected_match, lighting, skin_tone, glasses, angle, notes
```

`python scripts/bias_fairness_test.py --self-test` runs the harness on synthetic data to confirm it works. No real fairness data has been collected yet.

---

## Getting started

### Prerequisites
- **Python 3.11** (developed and tested on 3.11.6, Windows). Other versions may not work with the pinned `mediapipe` and `tensorflow` versions.
- **FFmpeg and FFprobe** on your `PATH` (or set `FFMPEG_BIN` / `FFPROBE_BIN`). Without them the app runs, but incident clips cannot be built.
- A webcam and microphone, and a modern Chromium-based or Firefox browser.
- **Internet on the first run**, so DeepFace can download the SFace and YuNet weights.
- Camera access requires `localhost` or HTTPS.

### Install

```bash
git clone <your-repo-url>
cd Ai-proctoring-system

python -m venv venv
# Windows (PowerShell):
venv\Scripts\Activate.ps1
# macOS / Linux:
source venv/bin/activate

python -m pip install -r requirements.txt
```

> Use `python -m pip` rather than `pip` directly. If you move the project folder after creating the virtual environment, plain `pip` can fail with "Fatal error in launcher".

Optional, for incident-clip captions (also listed, commented out, at the bottom of `requirements.txt`):

```bash
python -m pip install faster-whisper
```

### Run

```bash
python app.py
```

The app is served at `http://127.0.0.1:5000`.

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

---

## Configuration

The defaults in `config.py` are development placeholders. Set real secrets before using the app with anyone else:

```bash
# Windows (PowerShell)
$env:SECRET_KEY = "a-long-random-string"
$env:JWT_SECRET_KEY = "another-long-random-string"

# macOS / Linux
export SECRET_KEY="a-long-random-string"
export JWT_SECRET_KEY="another-long-random-string"
```

Key settings and where to find them:

| Setting | Value | Location |
|---|---|---|
| Identity threshold (cosine distance) | 0.593 | `detection/face_authentication.py` |
| Recognition model / detector | SFace / YuNet | `detection/face_authentication.py` |
| CLAHE dark cutoff (mean lightness) | 130 | `detection/face_authentication.py` |
| Token expiry | 2 hours | `config.py` |
| Login lockout | 5 failures, 30 seconds | `config.py` |
| Abandoned-session timeout | 180 seconds | `config.py` |
| Passive liveness | Off (`PASSIVE_LIVENESS_ENABLED = False`) | `detection/engine.py` |

---

## Usage walkthrough

1. Register an admin at `/admin/register`, then log in at `/adlogin`.
2. Register a student at `/register` and note the generated student ID (for example `STU-1234`).
3. As admin, create an exam and add that student ID to the enrolled list.
4. Log in as the student at `/`, open the exam, pass the liveness and identity checks, and take the exam.
5. As admin, open **Session register**, expand the student, and review incidents.

---

## Evaluation so far

Evaluation was done by one tester (the identity-recognition owner) with a single webcam. **These results are indicative only, not statistically valid.**

- **Pre-exam identity attempts:** 8 logged attempts from one student account. 4 passed (distances 0.357 to 0.557) and 4 failed (0.600 to 0.693). The two groups sit close to the 0.593 threshold, so the system is sensitive near the boundary. The log does not record who was in front of the camera, so this does **not** give false-accept or false-reject rates.
- **Lighting (manual, CLAHE off):** brightness 136 to 141 worked well; 102 to 112 worked but liveness struggled; 81 and below hardly worked.
- **CLAHE:** implemented with a cutoff of 130. Always-on CLAHE failed 10 of 10 attempts at about 130 brightness (cause not isolated; photos were enrolled without CLAHE, so an enrolment/live mismatch is possible), which is why it is gated. Gated results were inconclusive because the gate setting at test time was not recorded. **CLAHE is added but not yet proven to help.**
- **Threshold:** 0.593 is the SFace library default. It was **not tuned**, because no impostor data has been collected.
- **Latency:** per-frame processing time was not measured. The system is lightweight by design, but we make no speed claims.

---

## Known limitations

- **Authentication is not enforced on the API.** Login issues a JWT, but no route requires it and the frontend does not send it. Admin endpoints and admin registration are reachable without logging in. Fix before any real deployment.
- **Face verification is unvalidated.** The threshold has not been tuned or tested across lighting, skin tone, glasses or head angle. Treat identity flags as prompts for a human to look, not as proof.
- **Fairness testing has not been run.** The harness exists but no real photos have been collected. It also enrols one photo per person (the system uses three) and tests still photos rather than webcam frames.
- **CLAHE is unproven** (see [Evaluation so far](#evaluation-so-far)).
- **Mid-exam distances are not stored.** Only the pass/fail flag is saved, and the `live_snapshot_path` column of `VerificationAttempt` is never populated.
- **Exam start time and duration are not enforced.** A student can open any upcoming exam at any time, and nothing ends the exam when time runs out.
- **The exam content is a demo.** The 15 questions are hard-coded in `templates/index.html` and answers are not stored.
- **Camera problems are not shown to admins.** Failures are logged in a table but not displayed or flagged.
- **Refreshing the exam page restarts the recording timeline**, which can overwrite earlier segment records for that session.
- **Broken route:** `templates/index.html` redirects to a non-existent `/verify.html`; only `/verify` is registered.
- **Login page filename case.** `app.py` loads `Student_login.html`, but the file is `student_login.html`. Works on Windows and macOS, fails on Linux.
- **Unused dependencies.** Flask-SocketIO is initialised but not used, and `requirements.txt` was generated with `pip freeze`, so it also lists transitive dependencies and a few packages the app may not use directly (for example `gunicorn`, `sounddevice` and `matplotlib`). It has not been pruned.
- **SQLite and a single process.** Suitable for development and small trials only.
- **Head-pose detection can be confused with absence** at extreme angles, because the face model may lose tracking at a profile view. The landmark model tracks at most two faces.
- **Captions** depend on audio quality and run locally, so they are slow on weak machines.

---

## Future work

1. **Fairness and bias testing** with real photos across lighting, skin tone, glasses and angle, then tune the threshold from the results.
2. **Validate CLAHE** with a controlled comparison (same enrolment, gate setting recorded) and consider enrolling with the same enhancement.
3. **Passive (continuous) liveness.** Implemented but switched off; needs re-tuning for the real frame rate.
4. **Tab-switch evidence:** snapshot on switch and record which tab was opened.
5. **Exam enrolment flow:** let students register for exams instead of admins typing IDs.
6. **Enforce authentication and roles** on every API route and page.
7. **Enforce exam timing** (start window, countdown, automatic submission) and store real answers.
8. **Surface recording and camera issues** to admins.
9. **Automated tests** for detection logic, session lifecycle and API.
10. **Measure latency** and store mid-exam distances for analysis.

---

## Slides and report

Project deliverables are in the [`docs/`](docs/) folder:

| File | Description |
|---|---|
| `docs/AI_Proctoring_System_2_0_Group_1_Presentation.pptx` | Group presentation slides |
| `docs/AI_Proctoring_System_2.0_Paper.docx` | Project report |

---

## Protecting student data

The system handles biometric and video data. **Do not commit it.** Make sure `.gitignore` covers at least:

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

## Team

**Group 1**

| Member | Area |
|---|---|
| [Miracle Okoli](https://github.com/YOUR-USERNAME) | Face recognition (DeepFace/SFace), CLAHE, fairness-testing harness |
| [Chetanna Igboelina](https://github.com/USERNAME) | Liveness detection |
| [Fouad Abdulhakeem](https://github.com/USERNAME) | Mid-exam identity re-verification |
| [Faithful Sayo](https://github.com/USERNAME) | Login lockout, token expiry, idle logout, abandoned-session handling |
| [Philip Abbah](https://github.com/USERNAME) | Recording |
| [Anosi Oshanor](https://github.com/USERNAME) | Incident handling and clips |
| [Victor Asuquo](https://github.com/USERNAME) | Storage and backend |
| [Ibrahim Abdullahi](https://github.com/USERNAME) | Admin video playback, jump-to-event buttons, session register |
| [Gift Umoh](https://github.com/USERNAME) | Timestamps |

---

## Acknowledgements

- **[NCAIR](https://ncair.nitda.gov.ng/)**, our IT institution, for the course and learning environment.
- **Victor Rizama** ([@victor-rizama](https://github.com/USERNAME)), facilitator and project supervisor.
- **Stephen Ayuba** ([@stephen-ayuba](https://github.com/USERNAME)), facilitator and project supervisor.
- **[Joelokolia12](https://github.com/Joelokolia12)** for the original V1 project this work extends.
- The open-source projects we build on: Flask, MediaPipe, OpenCV, DeepFace (SFace, YuNet), FFmpeg and faster-whisper.

---

## License

Built as an academic course project at NCAIR. Not intended for production use without further security hardening and validation (see [Known limitations](#known-limitations)). Add a `LICENSE` file if you want to allow reuse; until then, all rights remain with the authors, and the V1 baseline remains subject to its own license.
