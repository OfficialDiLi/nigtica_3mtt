# Nigtica

Nigtica lets you speak and listen in Nigerian languages — Yoruba, Igbo, Hausa,
Nigerian Pidgin, and English — right from your browser.

## Why I built this

Try asking a popular voice assistant to read Yoruba out loud and you'll hear
the problem immediately: wrong accent, missing tones, words that sound nothing
like how your mother says them. Pidgin? Most tools don't even try.

Meanwhile the people who need voice tech most — a blind student trying to
study, a trader who'd rather listen than read — get English or nothing. That
didn't sit right with me, so I built Nigtica: pick the engine that actually
speaks your language (YarnGPT for Yoruba, Igbo, Hausa, and Pidgin; Groq
Whisper for English and the rest), type or talk, and get an answer back in a
voice that sounds like home.

## Who it's for

- **Blind and visually impaired people** — hear any text read aloud in your
  own language, instead of screen readers that butcher Yoruba, Igbo, Hausa,
  or Pidgin.
- **Students** — listen to your notes, dictate instead of type, study in the
  language you actually think in.
- **Builders** — one place for Nigerian-language voice, without wiring up
  three different providers yourself.

## Features

- **Text-to-speech** — type or pick a sample (Yoruba, Igbo, Hausa, Pidgin,
  English), choose from 16+ live voices, optional translation into 18 target
  languages, adjustable playback speed, downloadable audio.
- **Speech-to-text** — record from the microphone in the browser; Nigerian
  audio goes to YarnGPT, English and foreign audio to Groq Whisper with a
  spoken-language hint and instant results.
- **User accounts** — email/password signup and login, session auth, CSRF
  protection. The entire site requires login; only auth pages and `/health`
  are public. Accounts can self-delete (usage rows are anonymized, never
  removed for audit).
- **Transcription & synthesis history** — recent activity sidebar with
  per-item language labels.
- **Operational transparency** — upstream errors surface machine-readable
  codes (`INVALID_INPUT`, `VOICE_NOT_FOUND`, …) with field-level details and
  trace IDs, in the UI and the server log.

## Quickstart

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env   # fill in keys (see Configuration)
uv sync
uv run voices
# open http://localhost:5001
```

Then sign up at `/signup`. For evaluation or demos, set `DEMO_EMAIL` and
`DEMO_PASSWORD` to seed a ready-made account on startup. The public landing
page is `/` (with Sign up and Log in calls to action); the app itself lives
at `/app` and requires login.

## Configuration

All settings are environment variables (see `.env.example`):

| Variable           | Required | Description                                                        |
| ------------------ | -------- | ------------------------------------------------------------------ |
| `YARNGPT_API_KEY`  | Yes      | YarnGPT key (`sk_live_…`) — TTS and Nigerian-language STT          |
| `GROQ_API_KEY`     | No       | Groq key (`gsk_…`) — enables English/foreign-language STT          |
| `SECRET_KEY`       | Yes      | Flask session signing key (server refuses ephemeral keys in prod)  |
| `YARNGPT_BASE_URL` | No       | Override, defaults to `https://api.yarngpt.ai/api/v1`              |
| `GROQ_BASE_URL`    | No       | Override, host only (the SDK appends `/openai/v1`)                 |
| `GROQ_STT_MODEL`   | No       | Override, defaults to `whisper-large-v3-turbo`                     |
| `VOICES_DB`        | No       | SQLite path, defaults to the Flask instance folder                 |
| `DEMO_EMAIL` / `DEMO_PASSWORD` | No | Seed a demo login on startup when both are set              |
| `PORT`             | No       | Defaults to `5001` (Render injects its own)                        |

Without a YarnGPT key the UI runs in fallback mode: pages render, voices list
empty with a hint, and synthesis/transcription calls explain what is missing.

## Architecture

```
browser (templates/index.html + static/app.js)
  │  fetch/FormData, X-CSRF-Token
  ▼
Flask (src/voices/main.py) ── session auth (src/voices/auth.py)
  ├── YarnGPT client (src/voices/client.py, async httpx)
  │     TTS: POST /tts + Idempotency-Key → poll GET /status/{id} → audio_url
  │     STT: POST /asr (multipart) → poll GET /asr/{id} → transcript
  └── Groq client (src/voices/groq_client.py, official SDK)
        STT: POST /audio/transcriptions → transcript (synchronous)
```

- **Engine routing:** Nigerian languages (`yo`, `ig`, `ha`, `pcm`) always use
  YarnGPT; Groq accepts English and foreign languages (`en`, `fr`, `es`,
  `de`, `ar`, `pt`, `sw`) and rejects Nigerian codes with a message pointing
  back to YarnGPT. If no voice is chosen, the YarnGPT catalogue default is
  resolved live (never hardcoded).
- **Upload limits:** 64 MiB overall, 25 MiB for Groq (free-tier cap).
- **Storage:** users in SQLite — a deliberate MVP choice (zero migrations,
  zero services). See Deploy for the persistence caveat.

## API reference

JSON endpoints (session cookie + `X-CSRF-Token` header required):

| Method | Path                    | Description                                              |
| ------ | ----------------------- | -------------------------------------------------------- |
| `GET`  | `/health`               | Liveness + `api_key_configured` flag (public)            |
| `GET`  | `/api/voices`           | Live YarnGPT voice catalogue                             |
| `GET`  | `/api/languages`        | YarnGPT ASR language coverage                            |
| `POST` | `/api/tts`              | Queue synthesis → `{job_id, status}`                     |
| `GET`  | `/api/tts-job/{id}`     | Poll synthesis → `{status, percentage, audio_url?}`      |
| `POST` | `/api/stt`              | Transcribe (`engine=yarngpt\|groq`) → job or transcript  |
| `GET`  | `/api/stt-job/{id}`     | Poll YarnGPT transcription → `{status, transcript, …}`   |

Errors share one shape: `{error, user_message, details?, trace_id?}` where
`error` is a stable code safe to branch on. Unauthenticated calls receive
`401 AUTH_REQUIRED`.

Legacy HTML-fragment routes (`POST /tts`, `/tts-status/…`, `/stt`,
`/stt-status/…`) are retained for progressive enhancement.

## Responsible AI

- **Transparency** — every result carries 👍/👎 feedback (stored per engine
  and language for quality tracking); users see their own 7-day usage in the
  header and at `GET /api/usage`. Audio handling is disclosed on the STT
  card: provider transcription, only metadata stored.
- **Privacy** — passwords hashed, no audio/text stored server-side (audit log
  keeps metadata only: action, engine, size, job id), self-serve account
  deletion with anonymized history.
- **Safety** — login-gated synthesis, per-user hourly caps
  (`RATE_LIMIT_PER_HOUR`, default 20, `429 RATE_LIMITED`), input and upload
  size limits, idempotency keys against double billing.
- **Ethical use** — public `/terms` (acceptable use, AI limitations, data,
  enforcement); signup requires explicit acceptance, timestamped per user.

## Deploy (Render)

`render.yaml` defines the service: native Python runtime, `uv sync --frozen`
build, Gunicorn (`voices.main:app`, 2 workers, 150 s timeout to cover long
transcriptions), health check on `/health`. Set `YARNGPT_API_KEY` (and
`GROQ_API_KEY`) in the dashboard; `SECRET_KEY` is generated.

> **Persistence note:** Render's free tier has an ephemeral filesystem and no
> disks, so registered users are wiped on restarts — acceptable for a demo
> URL. Spin-down is handled: the app self-pings `GET /health` every 9 minutes
> through its public URL (Render provides `RENDER_EXTERNAL_URL`
> automatically), so the 15-minute idle timer never trips. For real users, use
> a paid plan with a persistent disk and point `VOICES_DB` under its mount
> (e.g. `/var/data/voices.db`).

## Development

```bash
uv sync
uv run voices          # dev server on :5001
```

Conventions: per-request settings reads (a late `.env` needs no restart),
branch on upstream error codes rather than message text, and never hardcode
voice IDs — the YarnGPT catalogue is the source of truth.

## Credits

Speech AI by [YarnGPT](https://yarngpt-web.azurewebsites.net/api-docs) and
[Groq](https://console.groq.com/docs/speech-to-text). 