"""Local email/password auth: SQLite users, session login, CSRF tokens.

No extra dependencies — password hashing comes from Werkzeug (via Flask),
storage from stdlib sqlite3.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from functools import wraps

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS usage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  action TEXT NOT NULL,
  engine TEXT NOT NULL DEFAULT '',
  units INTEGER NOT NULL DEFAULT 0,
  job_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS feedback_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  kind TEXT NOT NULL,
  engine TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT '',
  vote TEXT NOT NULL,
  job_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class AuthError(ValueError):
    """User-facing auth failure (message is safe to display)."""


def get_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = [r["name"] for r in db.execute("PRAGMA table_info(users)")]
    if "terms_accepted_at" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN terms_accepted_at TEXT")
    db.commit()
    return db


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def create_user(db: sqlite3.Connection, email: str | None, password: str | None) -> int:
    email = normalize_email(email)
    if not EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address.")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise AuthError("Password must be at least 8 characters.")
    try:
        cur = db.execute(
            "INSERT INTO users (email, password_hash, terms_accepted_at)"
            " VALUES (?, ?, datetime('now'))",
            (email, generate_password_hash(password or "")),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise AuthError("An account with that email already exists.") from None
    return cur.lastrowid


def verify_user(
    db: sqlite3.Connection, email: str | None, password: str | None
) -> sqlite3.Row | None:
    row = db.execute(
        "SELECT * FROM users WHERE email = ?", (normalize_email(email),)
    ).fetchone()
    if row and check_password_hash(row["password_hash"], password or ""):
        return row
    return None


def current_user_email() -> str | None:
    return session.get("user_email")


def csrf_token() -> str:
    tok = session.get("csrf_token")
    if not tok:
        tok = secrets.token_hex(16)
        session["csrf_token"] = tok
    return tok


def csrf_valid(value: str | None) -> bool:
    expected = session.get("csrf_token", "")
    return bool(value) and bool(expected) and secrets.compare_digest(str(value), str(expected))


def safe_next(value: str | None, fallback: str = "/") -> str:
    """Allow only relative paths (no open redirects)."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


def log_event(
    db: sqlite3.Connection,
    user_id: int,
    action: str,
    engine: str = "",
    units: int = 0,
    job_id: str = "",
) -> None:
    """Record usage metadata (never audio or text)."""
    db.execute(
        "INSERT INTO usage_events (user_id, action, engine, units, job_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_id, action, engine, units, job_id or ""),
    )
    db.commit()


def recent_count(
    db: sqlite3.Connection, user_id: int, prefix: str, hours: int = 1
) -> int:
    """Accepted actions under `prefix` (e.g. 'tts', 'stt') in the last N hours."""
    row = db.execute(
        "SELECT COUNT(*) AS c FROM usage_events"
        " WHERE user_id = ? AND action LIKE ?"
        f" AND created_at >= datetime('now', '-{int(hours)} hours')",
        (user_id, prefix + "%"),
    ).fetchone()
    return int(row["c"])


def usage_summary(db: sqlite3.Connection, user_id: int, days: int = 7) -> dict:
    """Own accepted-use counts per engine family over the last N days."""
    rows = db.execute(
        "SELECT"
        " SUM(CASE WHEN action LIKE 'tts%' THEN 1 ELSE 0 END) AS tts,"
        " SUM(CASE WHEN action LIKE 'stt%' THEN 1 ELSE 0 END) AS stt"
        " FROM usage_events WHERE user_id = ?"
        f" AND created_at >= datetime('now', '-{int(days)} days')",
        (user_id,),
    ).fetchone()
    return {"tts": int(rows["tts"] or 0), "stt": int(rows["stt"] or 0)}


def save_feedback(
    db: sqlite3.Connection,
    user_id: int,
    kind: str,
    engine: str,
    language: str,
    vote: str,
    job_id: str = "",
) -> None:
    if kind not in ("tts", "stt") or vote not in ("up", "down"):
        raise AuthError("Feedback needs kind tts|stt and vote up|down.")
    db.execute(
        "INSERT INTO feedback_events (user_id, kind, engine, language, vote, job_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, kind, engine[:32], language[:16], vote, job_id[:64]),
    )
    db.commit()


def delete_user(db: sqlite3.Connection, user_id: int) -> None:
    """Delete the account; anonymize its usage/feedback rows."""
    db.execute("UPDATE usage_events SET user_id = NULL WHERE user_id = ?", (user_id,))
    db.execute("UPDATE feedback_events SET user_id = NULL WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()


def login_required(view):
    """Gate a route on session login (+ CSRF on POST).

    API routes answer 401/403 JSON; page/fragment routes redirect / 400.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return (
                    jsonify(error="AUTH_REQUIRED", user_message="Log in to continue."),
                    401,
                )
            return redirect(url_for("login", next=request.path))
        if request.method == "POST":
            token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
            if not csrf_valid(token):
                if request.path.startswith("/api/"):
                    return (
                        jsonify(
                            error="CSRF_FAILED",
                            user_message="Session expired — reload the page and try again.",
                        ),
                        403,
                    )
                return "Bad CSRF token — reload the page and try again.", 400
        return view(*args, **kwargs)

    return wrapper
