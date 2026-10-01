import os
from datetime import timedelta

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-change-this')
    SQLALCHEMY_DATABASE_URI = 'sqlite:///proctoring.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'dev-jwt-secret-change-this')

    # --- Task 5, Part B: session timeout ---
    # Explicit now instead of relying on flask-jwt-extended's silent
    # 15-minute default. An exam can easily run longer than that, so this
    # needs to comfortably cover your longest scheduled exam duration.
    # Tune with the team; 2 hours is just a reasonable starting point.
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=2)

    # --- Task 5, Part A: login rate-limiting ---
    # After this many failed attempts on one account, the account is
    # locked for LOGIN_LOCKOUT_MINUTES. Starting values, not tuned —
    # adjust if the team finds them too strict or too loose.
    MAX_LOGIN_ATTEMPTS = 5
    LOGIN_LOCKOUT_MINUTES = 0.5

    # --- Abandoned-session detection (see session_cleanup.py) ---
    # The exam page posts a frame every ~3s, which doubles as a heartbeat.
    # Browsers throttle timers in background tabs (down to about once a
    # minute), so this must stay well above that or a student who merely
    # switches tabs would look disconnected. A wrongly-marked session
    # recovers automatically on the next heartbeat, but avoid the flapping.
    SESSION_HEARTBEAT_TIMEOUT_SECONDS = 180
    SESSION_SWEEP_INTERVAL_SECONDS = 30