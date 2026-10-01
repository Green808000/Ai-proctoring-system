#

# importing the tools we need 
import os

from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO
from flask_jwt_extended import JWTManager
from config import Config
from models import db
from session_cleanup import ensure_schema, start_sweeper

# web app creation and connection
app = Flask(__name__)
app.config.from_object(Config)

# connecting database 
db.init_app(app)

# login tokens
jwt = JWTManager(app)


# --- Task 5, Part B: session timeout ---
# What should happen when a token expires mid-exam, decided rather than
# left to whatever flask-jwt-extended does by default: a clear JSON
# error the frontend can detect and show a "please log in again" message
# for, instead of a confusing generic failure.
#
# IMPORTANT CAVEAT: these only fire on a route that actually checks the
# token (i.e. is decorated with @jwt_required()). As of this change, NO
# route in the app does that yet — create_access_token() is called at
# login and the token is stored client-side, but nothing currently
# verifies it on any request. Wiring @jwt_required() onto the routes
# that should require it (and having the frontend send the token as an
# Authorization header) is a separate, bigger change outside Task 5's
# file scope (routes/auth.py, config.py) — flagging it here so the team
# can decide when to take it on.
@jwt.expired_token_loader
def handle_expired_token(jwt_header, jwt_payload):
    return jsonify({
        "error": "session_expired",
        "message": "Your session has expired. Please log in again."
    }), 401


@jwt.invalid_token_loader
def handle_invalid_token(reason):
    return jsonify({
        "error": "invalid_token",
        "message": "Your session is invalid. Please log in again."
    }), 401


@jwt.unauthorized_loader
def handle_missing_token(reason):
    return jsonify({
        "error": "unauthorized",
        "message": "Please log in to continue."
    }), 401

# used for real time messaging (live updating the admin dashboard)
socketio = SocketIO(app, cors_allowed_origins="*")

# Register Blueprints
from routes.auth import auth_bp
app.register_blueprint(auth_bp, url_prefix='/api/auth')

from routes.exam import exam_bp
app.register_blueprint(exam_bp, url_prefix='/api/exam')

from routes.proctor import proctor_bp
app.register_blueprint(proctor_bp, url_prefix='/api/proctor')

# Team 2 recording/incident subsystem. This uses separate tables so models.py stays protected.
from routes.team2 import team2_bp
from team2.store import init_schema as init_team2_schema
app.register_blueprint(team2_bp, url_prefix='/api/team2')

with app.app_context():
    db.create_all()
    ensure_schema()  # adds exam_session.last_heartbeat to older databases
    init_team2_schema()

# Connecting the main pages of the site
@app.route("/")
def student_home():
    return render_template("Student_login.html")

@app.route("/my-exams")
def my_exams_page():
    return render_template("my_exams.html")

@app.route("/register")
def register_page():
    return render_template("register.html")

@app.route("/admin")
def admin_home():
    return render_template("admin_dashboard.html")

@app.route("/adlogin")
def admin_login():
    return render_template("admin_login.html")

@app.route("/adreg")
def admin_reg():
    return render_template("admin_register.html")

@app.route("/admin/register")
def admin_register():
    return render_template("admin_register.html")

@app.route("/demo")
@app.route("/index.html")
def webcam_demo():
    return render_template("index.html")

@app.route('/verify')
def verify_page():
    return render_template('verify.html')


DEBUG = True

if __name__ == "__main__":
    # With debug=True Flask starts a reloader that runs this file twice; only
    # the child process (WERKZEUG_RUN_MAIN=true) serves requests, so start the
    # abandoned-session sweeper there to avoid running two of them.
    if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_sweeper(app)

    # Runs using SocketIO server for real-time webcams & flags
    socketio.run(app, host="0.0.0.0", debug=DEBUG, port=5000)