from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import os, json, threading, base64
from dotenv import load_dotenv
from bot import start_bot_for_user, stop_bot_for_user

load_dotenv()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-this")

database_url = os.environ.get("DATABASE_URL", "sqlite:///leetcode.db")
# Render (and Heroku) give postgres:// but SQLAlchemy needs postgresql://
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

if not os.environ.get("RENDER"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # dev only, localhost isn't https

# ─── MODELS ───────────────────────────────────────────────────────────────────

class User(db.Model):
    id               = db.Column(db.Integer, primary_key=True)
    email            = db.Column(db.String(120), unique=True, nullable=False)
    password         = db.Column(db.String(200), nullable=False)
    lc_session       = db.Column(db.Text, default="")
    lc_csrf          = db.Column(db.Text, default="")
    gmail_token      = db.Column(db.Text, default="")   # JSON string
    bot_enabled      = db.Column(db.Boolean, default=False)
    logs             = db.relationship("Log", backref="user", lazy=True)

class Log(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    user_id   = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    message   = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, server_default=db.func.now())

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def logged_in():
    return "user_id" in session

def current_user():
    return User.query.get(session["user_id"]) if logged_in() else None

def add_log(user_id, message):
    with app.app_context():
        db.session.add(Log(user_id=user_id, message=message))
        db.session.commit()

# ─── AUTH ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("dashboard") if logged_in() else url_for("login"))

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email    = request.form["email"].strip().lower()
        password = request.form["password"]
        if User.query.filter_by(email=email).first():
            return render_template("signup.html", error="Email already registered.")
        user = User(email=email, password=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        session["user_id"] = user.id
        return redirect(url_for("setup"))
    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form["email"].strip().lower()
        password = request.form["password"]
        user     = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password, password):
            return render_template("login.html", error="Invalid email or password.")
        session["user_id"] = user.id
        return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    user = current_user()
    if user:
        stop_bot_for_user(user.id)
    session.clear()
    return redirect(url_for("login"))

# ─── SETUP ────────────────────────────────────────────────────────────────────

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if not logged_in():
        return redirect(url_for("login"))
    user = current_user()
    if request.method == "POST":
        user.lc_session = request.form["lc_session"].strip()
        user.lc_csrf    = request.form["lc_csrf"].strip()
        db.session.commit()
        return redirect(url_for("gmail_auth"))
    return render_template("setup.html", user=user)

# ─── GMAIL OAUTH ──────────────────────────────────────────────────────────────

@app.route("/gmail/auth")
def gmail_auth():
    if not logged_in():
        return redirect(url_for("login"))
    flow = Flow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=url_for("gmail_callback", _external=True),
    )
    auth_url, state = flow.authorization_url(prompt="consent")
    session["oauth_state"] = state
    return redirect(auth_url)

@app.route("/gmail/callback")
def gmail_callback():
    if not logged_in():
        return redirect(url_for("login"))
    flow = Flow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        state=session["oauth_state"],
        redirect_uri=url_for("gmail_callback", _external=True),
    )
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    user  = current_user()
    user.gmail_token = creds.to_json()
    db.session.commit()
    return redirect(url_for("dashboard"))

# ─── DASHBOARD ────────────────────────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    if not logged_in():
        return redirect(url_for("login"))
    user = current_user()
    logs = Log.query.filter_by(user_id=user.id).order_by(Log.timestamp.desc()).limit(50).all()
    return render_template("dashboard.html", user=user, logs=logs)

# ─── BOT TOGGLE ───────────────────────────────────────────────────────────────

@app.route("/bot/toggle", methods=["POST"])
def bot_toggle():
    if not logged_in():
        return jsonify({"error": "not logged in"}), 401
    user = current_user()
    if not user.lc_session or not user.lc_csrf:
        return jsonify({"error": "Please complete setup first."}), 400
    if not user.gmail_token:
        return jsonify({"error": "Please connect Gmail first."}), 400

    user.bot_enabled = not user.bot_enabled
    db.session.commit()

    if user.bot_enabled:
        start_bot_for_user(user.id, user.email, user.lc_session, user.lc_csrf, user.gmail_token, add_log)
    else:
        stop_bot_for_user(user.id)

    return jsonify({"enabled": user.bot_enabled})

# ─── UPDATE COOKIES ───────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET", "POST"])
def settings():
    if not logged_in():
        return redirect(url_for("login"))
    user = current_user()
    if request.method == "POST":
        user.lc_session = request.form["lc_session"].strip()
        user.lc_csrf    = request.form["lc_csrf"].strip()
        db.session.commit()
        return render_template("settings.html", user=user, success="Cookies updated!")
    return render_template("settings.html", user=user)

# ─── ENTRY ────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)