"""Flask UI for the YarnGPT TTS/STT demo.

Jinja template lives in ``templates/index.html``; CSS/JS in ``static/``
(standard Flask layout). The page talks to the JSON routes below:

- TTS: ``POST /api/tts`` queues synthesis, frontend polls
  ``GET /api/tts-job/<job_id>`` and plays ``audio_url``.
- STT: MediaRecorder captures mic audio (audio-only WebM, accepted by YarnGPT),
  ``POST /api/stt`` uploads it, frontend polls ``GET /api/stt-job/<job_id>``.

The API key is read per request (``_settings``) so creating ``.env`` later
takes effect without restarting the server.

Reference: https://yarngpt-web.azurewebsites.net/api-docs
"""

from __future__ import annotations

import asyncio
import html
import os
import secrets
import threading

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from voices import auth
from voices.auth import AuthError
from voices.client import YarnGPTClient, YarnGPTError
from voices.config import (
    GROQ_LANGUAGES,
    GROQ_MAX_UPLOAD_BYTES,
    NIGERIAN_LANGUAGES,
    PORT,
    SAMPLE_ORDER,
    SAMPLE_TEXTS,
    TARGET_LANGUAGES,
    TTS_FORMATS,
)
from voices.groq_client import GroqClient, GroqError

load_dotenv()

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "")
if not app.secret_key:
    app.secret_key = secrets.token_hex(32)
    print(
        "[voices] WARNING: SECRET_KEY not set — using an ephemeral key "
        "(logins reset on restart). Set SECRET_KEY in .env."
    )

DB_PATH = os.getenv("VOICES_DB", os.path.join(app.instance_path, "voices.db"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def _keepalive_ping(base_url: str) -> bool:
    """Single keep-alive ping against the public URL (counts as inbound traffic)."""
    import urllib.request

    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/health", timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as err:  # never let the pinger crash the app
        print(f"[keepalive] ping failed: {err}")
        return False


def _keepalive_loop(base_url: str, interval_s: int) -> None:
    import time

    while True:
        _keepalive_ping(base_url)
        time.sleep(interval_s)


def _start_keepalive() -> None:
    """Self-ping the public URL so Render's idle timer never trips.

    Needs RENDER_EXTERNAL_URL (provided by Render) or KEEPALIVE_URL.
    Note: runs once per Gunicorn worker — duplicate pings are harmless.
    """
    base_url = os.getenv("KEEPALIVE_URL", "") or os.getenv("RENDER_EXTERNAL_URL", "")
    if not base_url or os.getenv("PYTEST_CURRENT_TEST"):
        return
    try:
        interval_s = max(60, int(os.getenv("KEEPALIVE_INTERVAL_MIN", "9")) * 60)
    except ValueError:
        interval_s = 9 * 60
    thread = threading.Thread(
        target=_keepalive_loop, args=(base_url, interval_s),
        name="keepalive", daemon=True,
    )
    thread.start()
    print(f"[keepalive] pinging {base_url.rstrip('/')}/health every {interval_s // 60} min")


_start_keepalive()


def _seed_demo_user() -> None:
    """Create the demo login if DEMO_EMAIL/DEMO_PASSWORD are set (challenge judging)."""
    email = os.getenv("DEMO_EMAIL", "").strip()
    password = os.getenv("DEMO_PASSWORD", "")
    if not email or not password:
        return None
    db = auth.get_db(DB_PATH)
    try:
        row = db.execute(
            "SELECT id FROM users WHERE email = ?", (auth.normalize_email(email),)
        ).fetchone()
        if row is None:
            try:
                auth.create_user(db, email, password)
                print(f"[voices] demo user created: {auth.normalize_email(email)}")
            except AuthError as err:
                print(f"[voices] demo user not created: {err}")
    finally:
        db.close()


_seed_demo_user()

PUBLIC_ENDPOINTS = {
    "login",
    "login_submit",
    "signup",
    "signup_submit",
    "logout",
    "health",
    "static",
    "landing",
    "terms",
}


@app.before_request
def guard_site():
    """Everything except auth pages, /health, and static needs a login."""
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if not session.get("user_id"):
        if request.path.startswith("/api/"):
            return (
                jsonify(error="AUTH_REQUIRED", user_message="Log in to continue."),
                401,
            )
        return redirect(url_for("login", next=request.path))
    return None

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
DEFAULT_BASE_URL = "https://api.yarngpt.ai/api/v1"


def _settings() -> tuple[str, str]:
    """(api_key, base_url), re-read per request so a late .env just works."""
    load_dotenv()
    key = os.getenv("YARNGPT_API_KEY", "").strip().strip("'\"")
    base = os.getenv("YARNGPT_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL
    return key, base


def _groq_settings() -> tuple[str, str, str]:
    """(api_key, base_url, model) for Groq STT, re-read per request."""
    load_dotenv()
    key = os.getenv("GROQ_API_KEY", "").strip().strip("'\"")
    base = os.getenv("GROQ_BASE_URL", "https://api.groq.com").strip().rstrip("/") or "https://api.groq.com"
    model = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo").strip() or "whisper-large-v3-turbo"
    return key, base, model


def _groq_err(err: GroqError) -> dict:
    print(f"[groq] {err.code} http={err.status} details={err.details}")
    return {
        "error": err.code,
        "user_message": err.message or "Groq transcription failed.",
        "details": err.details,
    }


def _cap_exceeded(prefix: str) -> dict | None:
    """Hourly per-user cap; None when the request may proceed."""
    from voices.config import RATE_LIMIT_PER_HOUR

    db = auth.get_db(DB_PATH)
    try:
        n = auth.recent_count(db, session["user_id"], prefix, 1)
    finally:
        db.close()
    if n >= RATE_LIMIT_PER_HOUR:
        return {
            "error": "RATE_LIMITED",
            "user_message": (
                f"Hourly limit reached ({RATE_LIMIT_PER_HOUR}/hour) — try again later."
            ),
        }
    return None


def _log(user_id: int, action: str, engine: str = "", units: int = 0, job_id: str = "") -> None:
    db = auth.get_db(DB_PATH)
    try:
        auth.log_event(db, user_id, action, engine, units, job_id)
    finally:
        db.close()


def _err(err: YarnGPTError) -> dict:
    """Public JSON error — keeps code/details/trace_id so callers can act on it."""
    print(f"[yarngpt] {err.code} http={err.status} trace={err.trace_id} details={err.details}")
    return {
        "error": err.code,
        "user_message": err.user_message,
        "details": err.details,
        "trace_id": err.trace_id or "",
    }


@app.get("/terms")
def terms():
    return render_template("terms.html")


@app.get("/")
def landing():
    return render_template("landing.html")


@app.get("/app")
def index():
    db = auth.get_db(DB_PATH)
    try:
        usage = auth.usage_summary(db, session["user_id"], 7)
    finally:
        db.close()
    return render_template(
        "index.html",
        sample=SAMPLE_TEXTS["yo"],
        samples=[(code, label, SAMPLE_TEXTS[code]) for code, label in SAMPLE_ORDER],
        target_languages=TARGET_LANGUAGES,
        groq_languages=GROQ_LANGUAGES,
        formats=TTS_FORMATS,
        user_email=auth.current_user_email(),
        usage_total=usage["tts"] + usage["stt"],
        csrf_token=auth.csrf_token(),
    )


@app.get("/health")
def health():
    key, _ = _settings()
    return jsonify(ok=True, api_key_configured=bool(key))


@app.get("/api/voices")
def api_voices():
    key, base = _settings()
    if not key:
        return jsonify(voices=[], hint="Set YARNGPT_API_KEY to list voices.")

    async def go():
        async with YarnGPTClient(key, base) as client:
            return await client.list_voices()

    try:
        return jsonify(asyncio.run(go()))
    except YarnGPTError as err:
        return jsonify(_err(err))


@app.get("/api/languages")
def api_languages():
    key, base = _settings()
    if not key:
        return jsonify(languages=[], hint="Set YARNGPT_API_KEY to list ASR languages.")

    async def go():
        async with YarnGPTClient(key, base) as client:
            return await client.list_asr_languages()

    try:
        return jsonify(asyncio.run(go()))
    except YarnGPTError as err:
        return jsonify(_err(err))


@app.post("/api/tts")
@auth.login_required
def api_tts():
    if (cap := _cap_exceeded("tts")) is not None:
        return jsonify(cap), 429
    key, base = _settings()
    key, base = _settings()
    if not key:
        return jsonify(
            error="MISSING_API_KEY",
            user_message="Set YARNGPT_API_KEY in .env to enable synthesis.",
        )
    text = (request.form.get("text") or "").strip()
    if not text:
        return jsonify(error="INVALID_INPUT", user_message="Text must be non-empty.")
    voice = request.form.get("voice") or None
    output_format = request.form.get("output_format") or "mp3"
    target_language = request.form.get("target_language") or None

    async def go():
        async with YarnGPTClient(key, base) as client:
            return await client.tts_queue(
                text,
                voice=voice,
                output_format=output_format,
                target_language=target_language,
            )

    try:
        job = asyncio.run(go())
    except YarnGPTError as err:
        return jsonify(_err(err))
    _log(session["user_id"], "tts.queue", "yarngpt", len(text), job.get("job_id", ""))
    return jsonify(job_id=job.get("job_id"), status=job.get("status", "queued"))


@app.get("/api/tts-job/<job_id>")
def api_tts_job(job_id: str):
    key, base = _settings()
    if not key:
        return jsonify(
            error="MISSING_API_KEY",
            user_message="Set YARNGPT_API_KEY in .env to enable synthesis.",
        )

    async def go():
        async with YarnGPTClient(key, base) as client:
            return await client.tts_status(job_id)

    try:
        status = asyncio.run(go())
    except YarnGPTError as err:
        return jsonify(_err(err))
    out: dict = {
        "status": status.get("status", ""),
        "percentage": status.get("percentage", 0),
    }
    if out["status"] == "completed":
        out["audio_url"] = status.get("audio_url") or ""
    elif out["status"] == "failed":
        out["user_message"] = status.get("user_message") or "Synthesis failed."
    return jsonify(out)


@app.post("/api/stt")
@auth.login_required
def api_stt():
    if (cap := _cap_exceeded("stt")) is not None:
        return jsonify(cap), 429
    upload = request.files.get("file")
    blob = upload.read() if upload else b""
    if not blob:
        return jsonify(error="INVALID_INPUT", user_message="Uploaded file is empty.")
    if len(blob) > MAX_UPLOAD_BYTES:
        return jsonify(error="INVALID_INPUT", user_message="File exceeds the 64 MiB limit.")
    engine = (request.form.get("engine") or "yarngpt").strip().lower()
    if engine == "groq":
        return _api_stt_groq(
            blob,
            upload.filename or "recording",
            upload.content_type or "application/octet-stream",
            request.form.get("language") or "en",
        )
    key, base = _settings()
    if not key:
        return jsonify(
            error="MISSING_API_KEY",
            user_message="Set YARNGPT_API_KEY in .env to enable transcription.",
        )
    target_language = request.form.get("target_language") or None

    async def go():
        async with YarnGPTClient(key, base) as client:
            return await client.asr_upload(
                blob,
                upload.filename or "recording",
                upload.content_type or "application/octet-stream",
                target_language=target_language,
            )

    try:
        job = asyncio.run(go())
    except YarnGPTError as err:
        return jsonify(_err(err))
    _log(session["user_id"], "stt.upload", "yarngpt", len(blob), job.get("job_id", ""))
    return jsonify(
        job_id=job.get("job_id"),
        status=job.get("status", "queued"),
        billable_blocks=job.get("billable_blocks"),
        credits_reserved=job.get("credits_reserved"),
    )


def _api_stt_groq(blob: bytes, filename: str, content_type: str, language: str):
    """Groq STT: synchronous transcript, shaped like a completed YarnGPT job.

    English and foreign languages only — Nigerian languages are rejected so
    they stay on YarnGPT.
    """
    lang = (language or "en").strip().lower()
    if lang in NIGERIAN_LANGUAGES:
        return jsonify(
            error="INVALID_INPUT",
            user_message=(
                "Groq is English and foreign languages only — "
                "use the YarnGPT engine for Yoruba, Igbo, Hausa, and Pidgin."
            ),
        )
    if len(blob) > GROQ_MAX_UPLOAD_BYTES:
        return jsonify(
            error="INVALID_INPUT",
            user_message="File exceeds Groq's 25 MB free-tier limit.",
        )
    key, base, model = _groq_settings()
    if not key:
        return jsonify(
            error="MISSING_GROQ_KEY",
            user_message="Set GROQ_API_KEY in .env to enable Groq transcription.",
        )

    async def go():
        async with GroqClient(key, base, model) as client:
            return await client.transcribe(blob, filename, content_type, language=language)

    try:
        result = asyncio.run(go())
    except GroqError as err:
        return jsonify(_groq_err(err))
    _log(session["user_id"], "stt.groq", "groq", len(blob), "")
    return jsonify(
        status="completed",
        engine="groq",
        transcript=result.get("text") or "",
        language=result.get("language") or "",
        model=model,
    )


@app.get("/api/stt-job/<job_id>")
def api_stt_job(job_id: str):
    key, base = _settings()
    if not key:
        return jsonify(
            error="MISSING_API_KEY",
            user_message="Set YARNGPT_API_KEY in .env to enable transcription.",
        )

    async def go():
        async with YarnGPTClient(key, base) as client:
            return await client.asr_status(job_id)

    try:
        status = asyncio.run(go())
    except YarnGPTError as err:
        return jsonify(_err(err))
    out: dict = {"status": status.get("status", "")}
    if out["status"] == "completed":
        out["transcript"] = status.get("transcript") or ""
        out["target_language"] = status.get("target_language")
        out["translation_status"] = status.get("translation_status")
        out["translated_transcript"] = status.get("translated_transcript") or ""
        out["credits_charged"] = status.get("credits_charged")
    elif out["status"] == "failed":
        out["error_message"] = status.get("error_message") or "Transcription failed."
        out["audio_available"] = status.get("audio_available", False)
    return jsonify(out)


# -- HTML-fragment routes (progressive enhancement, no JS needed) -------------


def _frag_error(action: str, err: YarnGPTError) -> str:
    detail = html.escape(err.user_message)
    if err.code in ("QUOTA_EXCEEDED", "SUBSCRIPTION_INACTIVE"):
        detail += " (see your YarnGPT Account page)"
    return (
        f'<div class="error-box"><p>{html.escape(action)} failed: '
        f"{html.escape(err.code)}</p><p><small>{detail}</small></p></div>"
    )


@app.post("/tts")
@auth.login_required
def tts_submit():
    if _cap_exceeded("tts") is not None:
        return '<div class="error-box"><p>Hourly limit reached — try again later.</p></div>', 429
    key, base = _settings()
    key, base = _settings()
    if not key:
        return '<div class="error-box"><p>Set YARNGPT_API_KEY in .env to enable synthesis.</p></div>'
    text = (request.form.get("text") or "").strip()
    if not text:
        return '<div class="error-box"><p>Text must be non-empty after trimming.</p></div>'

    async def go():
        async with YarnGPTClient(key, base) as client:
            return await client.tts_queue(
                text,
                voice=request.form.get("voice") or None,
                output_format=request.form.get("output_format") or "mp3",
                target_language=request.form.get("target_language") or None,
            )

    try:
        job = asyncio.run(go())
    except YarnGPTError as err:
        return _frag_error("Synthesis", err)
    _log(session["user_id"], "tts.queue", "yarngpt", len(text), job.get("job_id", ""))
    job_id = html.escape(job.get("job_id", ""))
    return (
        f"<div><p>Queued job {job_id} — polling for audio…</p>"
        f'<div hx-get="/tts-status/{job_id}" hx-trigger="load, every 2s"></div></div>'
    )


@app.get("/tts-status/<job_id>")
def tts_status_view(job_id: str):
    key, base = _settings()
    if not key:
        return '<div class="error-box"><p>Set YARNGPT_API_KEY in .env to enable synthesis.</p></div>'

    async def go():
        async with YarnGPTClient(key, base) as client:
            return await client.tts_status(job_id)

    try:
        status = asyncio.run(go())
    except YarnGPTError as err:
        if err.code == "JOB_NOT_FOUND":
            return '<div class="error-box"><p>Unknown job — it may belong to another account.</p></div>'
        return _frag_error("Status poll", err)
    state = status.get("status", "")
    if state == "completed":
        audio_url = status.get("audio_url") or ""
        if not audio_url:
            return '<div class="error-box"><p>Job completed but no audio_url was returned.</p></div>'
        safe = html.escape(audio_url, quote=True)
        return (
            f'<div><audio controls src="{safe}"></audio>'
            f'<p><a href="{safe}">Download audio</a></p></div>'
        )
    if state == "failed":
        msg = html.escape(status.get("user_message") or "No further detail.")
        return f'<div class="error-box"><p>Synthesis failed.</p><p><small>{msg}</small></p></div>'
    pct = status.get("percentage", 0)
    safe_id = html.escape(job_id, quote=True)
    return (
        f"<div><p>Status: {html.escape(str(state))} ({pct}%)…</p>"
        f'<div hx-get="/tts-status/{safe_id}" hx-trigger="every 2s"></div></div>'
    )


@app.post("/stt")
@auth.login_required
def stt_submit():
    if _cap_exceeded("stt") is not None:
        return '<div class="error-box"><p>Hourly limit reached — try again later.</p></div>', 429
    upload = request.files.get("file")
    blob = upload.read() if upload else b""
    if not blob:
        return '<div class="error-box"><p>Uploaded file is empty.</p></div>'
    if len(blob) > MAX_UPLOAD_BYTES:
        return '<div class="error-box"><p>File exceeds the 64 MiB limit.</p></div>'
    engine = (request.form.get("engine") or "yarngpt").strip().lower()
    if engine == "groq":
        if len(blob) > GROQ_MAX_UPLOAD_BYTES:
            return '<div class="error-box"><p>File exceeds Groq\'s 25 MB free-tier limit.</p></div>'
        lang = (request.form.get("language") or "en").strip().lower()
        if lang in NIGERIAN_LANGUAGES:
            return (
                '<div class="error-box"><p>Groq is English and foreign languages only — '
                "use the YarnGPT engine for Yoruba, Igbo, Hausa, and Pidgin.</p></div>"
            )
        key, base, model = _groq_settings()
        if not key:
            return '<div class="error-box"><p>Set GROQ_API_KEY in .env to enable Groq transcription.</p></div>'

        async def go_groq():
            async with GroqClient(key, base, model) as client:
                return await client.transcribe(
                    blob,
                    upload.filename or "recording",
                    upload.content_type or "application/octet-stream",
                    language=request.form.get("language") or "en",
                )

        try:
            result = asyncio.run(go_groq())
        except GroqError as err:
            print(f"[groq] {err.code} http={err.status} details={err.details}")
            return _frag_error("Groq transcription", YarnGPTError(err.code, err.message))
        _log(session["user_id"], "stt.groq", "groq", len(blob), "")
        return (
            "<div><h3>Transcript (Groq)</h3>"
            f"<blockquote>{html.escape(result.get('text') or '(silence)')}</blockquote></div>"
        )
    key, base = _settings()
    if not key:
        return '<div class="error-box"><p>Set YARNGPT_API_KEY in .env to enable transcription.</p></div>'

    async def go():
        async with YarnGPTClient(key, base) as client:
            return await client.asr_upload(
                blob,
                upload.filename or "upload",
                upload.content_type or "application/octet-stream",
                target_language=request.form.get("target_language") or None,
            )

    try:
        job = asyncio.run(go())
    except YarnGPTError as err:
        return _frag_error("Upload", err)
    _log(session["user_id"], "stt.upload", "yarngpt", len(blob), job.get("job_id", ""))
    job_id = html.escape(job.get("job_id", ""))
    quote = ""
    if job.get("billable_blocks") is not None:
        quote = (
            f" (quote: {job.get('billable_blocks')} block(s), "
            f"{job.get('credits_reserved')} credit(s) reserved)"
        )
    return (
        f"<div><p>Queued job {job_id}{html.escape(quote)} — polling for transcript…</p>"
        f'<div hx-get="/stt-status/{job_id}" hx-trigger="load, every 3s"></div></div>'
    )


@app.get("/stt-status/<job_id>")
def stt_status_view(job_id: str):
    key, base = _settings()
    if not key:
        return '<div class="error-box"><p>Set YARNGPT_API_KEY in .env to enable transcription.</p></div>'

    async def go():
        async with YarnGPTClient(key, base) as client:
            return await client.asr_status(job_id)

    try:
        status = asyncio.run(go())
    except YarnGPTError as err:
        if err.code == "JOB_NOT_FOUND":
            return '<div class="error-box"><p>Unknown job — it may belong to another account.</p></div>'
        return _frag_error("Status poll", err)
    state = status.get("status", "")
    if state == "completed":
        parts = [
            "<h3>Transcript</h3>",
            f"<blockquote>{html.escape(status.get('transcript') or '(silence)')}</blockquote>",
        ]
        if status.get("target_language"):
            if status.get("translation_status") == "completed":
                parts += [
                    f"<h3>Translation ({html.escape(str(status['target_language']))})</h3>",
                    f"<blockquote>{html.escape(status.get('translated_transcript') or '(empty)')}</blockquote>",
                ]
            else:
                parts.append(
                    f"<p><small>Translation: {html.escape(str(status.get('translation_status')))}</small></p>"
                )
        if status.get("credits_charged") is not None:
            parts.append(
                f"<p><small>Charged: {status['credits_charged']} credit(s).</small></p>"
            )
        return "<div>" + "".join(parts) + "</div>"
    if state == "failed":
        retry = (
            " You can retry this upload."
            if status.get("audio_available")
            else " The audio was cleaned up — upload again."
        )
        msg = html.escape((status.get("error_message") or "No further detail.") + retry)
        return f'<div class="error-box"><p>Transcription failed.</p><p><small>{msg}</small></p></div>'
    safe_id = html.escape(job_id, quote=True)
    return (
        f"<div><p>Status: {html.escape(str(state))} — transcribing (indeterminate, keep polling)…</p>"
        f'<div hx-get="/stt-status/{safe_id}" hx-trigger="every 3s"></div></div>'
    )


# -- feedback / usage / account --------------------------------------------


@app.post("/api/feedback")
@auth.login_required
def api_feedback():
    data = request.get_json(silent=True) or {}
    db = auth.get_db(DB_PATH)
    try:
        try:
            auth.save_feedback(
                db,
                session["user_id"],
                data.get("kind", ""),
                data.get("engine", ""),
                data.get("language", ""),
                data.get("vote", ""),
                data.get("job_id", ""),
            )
        except AuthError as err:
            return jsonify(error="INVALID_INPUT", user_message=str(err)), 400
    finally:
        db.close()
    return jsonify(ok=True)


@app.get("/api/usage")
@auth.login_required
def api_usage():
    db = auth.get_db(DB_PATH)
    try:
        summary = auth.usage_summary(db, session["user_id"], 7)
    finally:
        db.close()
    return jsonify(summary)


@app.post("/api/account/delete")
@auth.login_required
def api_account_delete():
    db = auth.get_db(DB_PATH)
    try:
        auth.delete_user(db, session["user_id"])
    finally:
        db.close()
    session.clear()
    return jsonify(ok=True)


@app.get("/login")
def login():
    if auth.current_user_email():
        return redirect(auth.safe_next(request.args.get("next", "/app"), "/app"))
    return render_template(
        "login.html", error="", csrf_token=auth.csrf_token(),
        next=request.args.get("next", "/app"),
    )


@app.post("/login")
def login_submit():
    if not auth.csrf_valid(request.form.get("csrf_token")):
        return render_template(
            "login.html", error="Session expired — reload and try again.",
            csrf_token=auth.csrf_token(), next=request.values.get("next", "/app"),
        ), 400
    db = auth.get_db(DB_PATH)
    try:
        row = auth.verify_user(db, request.form.get("email"), request.form.get("password"))
    finally:
        db.close()
    if row is None:
        return render_template(
            "login.html", error="Wrong email or password.",
            csrf_token=auth.csrf_token(), next=request.values.get("next", "/app"),
        ), 401
    session["user_id"] = row["id"]
    session["user_email"] = row["email"]
    return redirect(auth.safe_next(request.values.get("next", "/app"), "/app"))


@app.get("/signup")
def signup():
    if auth.current_user_email():
        return redirect("/app")
    return render_template(
        "signup.html", error="", csrf_token=auth.csrf_token(),
    )


@app.post("/signup")
def signup_submit():
    if not auth.csrf_valid(request.form.get("csrf_token")):
        return render_template(
            "signup.html", error="Session expired — reload and try again.",
            csrf_token=auth.csrf_token(),
        ), 400
    if request.form.get("terms") != "yes":
        return render_template(
            "signup.html", error="You must accept the Terms of Use to sign up.",
            csrf_token=auth.csrf_token(),
        ), 400
    if (request.form.get("password") or "") != (request.form.get("confirm") or ""):
        return render_template(
            "signup.html", error="Passwords do not match.",
            csrf_token=auth.csrf_token(),
        ), 400
    db = auth.get_db(DB_PATH)
    try:
        try:
            user_id = auth.create_user(
                db, request.form.get("email"), request.form.get("password")
            )
        except AuthError as err:
            return render_template(
                "signup.html", error=str(err), csrf_token=auth.csrf_token(),
            ), 400
    finally:
        db.close()
    session["user_id"] = user_id
    session["user_email"] = auth.normalize_email(request.form.get("email"))
    return redirect("/app")


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/")


def main() -> None:
    app.run(host="0.0.0.0", port=PORT)
