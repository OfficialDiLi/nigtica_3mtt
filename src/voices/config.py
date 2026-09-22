"""Runtime configuration for the Nigtica demo (Flask UI x YarnGPT API)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

YARNGPT_API_KEY: str = os.getenv("YARNGPT_API_KEY", "").strip().strip("'\"")
YARNGPT_BASE_URL: str = os.getenv(
    "YARNGPT_BASE_URL", "https://api.yarngpt.ai/api/v1"
).rstrip("/")

PORT: int = int(os.getenv("PORT", "5001"))

#: Per-user accepted synthesis/transcription actions per hour (abuse + cost guard).
RATE_LIMIT_PER_HOUR: int = int(os.getenv("RATE_LIMIT_PER_HOUR", "20"))

TTS_FORMATS: tuple[str, ...] = ("mp3", "wav")

POLL_INTERVAL_S: float = 1.0
POLL_TIMEOUT_S: float = 120.0

#: Sample texts for the demo UI (Yoruba, Igbo, Hausa, Pidgin, English).
SAMPLE_TEXTS: dict[str, str] = {
    "yo": "Ẹ kú àárọ̀. Káàárọ̀ sí Nigtica.",
    "ig": "Nnọọ. Daalụ maka iji Nigtica.",
    "ha": "Sannu. Barka da zuwa Nigtica.",
    "pcm": "How far? Welcome to Nigtica.",
    "en": "Hello. Welcome to Nigtica.",
}

#: Display order/labels for the sample chips above the TTS textbox.
SAMPLE_ORDER: tuple[tuple[str, str], ...] = (
    ("yo", "Yoruba"),
    ("ig", "Igbo"),
    ("ha", "Hausa"),
    ("pcm", "Pidgin"),
    ("en", "English"),
)

GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip().strip("'\"")
GROQ_BASE_URL: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com").rstrip("/")
GROQ_STT_MODEL: str = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")

#: Free-tier upload cap on Groq STT (25 MB); our own cap stays 64 MiB for YarnGPT.
GROQ_MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024

#: Spoken-language options for Groq STT (ISO-639-1). Groq is English and
#: foreign languages only — Nigerian languages (yo/ig/ha/pcm) are rejected
#: server-side and routed to YarnGPT instead.
GROQ_LANGUAGES: tuple[str, ...] = ("en", "fr", "es", "de", "ar", "pt", "sw")
NIGERIAN_LANGUAGES: tuple[str, ...] = ("yo", "ig", "ha", "pcm")
GROQ_LANGUAGE_MAP: dict[str, str] = {
    "yo": "yo",
    "ig": "ig",
    "ha": "ha",
    "pcm": "en",
    "en": "en",
    "fr": "fr",
    "es": "es",
    "de": "de",
    "ar": "ar",
    "pt": "pt",
    "sw": "sw",
}

#: Initial prompts bias spelling/style (same language as the audio, ≤224 tokens).
GROQ_PROMPTS: dict[str, str] = {
    "yo": "Èdè Yorùbá ni a ń sọ níbí. Kọ ọrọ náà pẹ̀lú àmì ohùn tó péye.",
    "ig": "A na-asụ asụsụ Igbo ebe a. Dee ihe a nụrụ nke ọma.",
    "ha": "Ana magana da Hausa a nan. Rubuta abin da aka ji dalla-dalla.",
    "en": "Nigerian English conversation. Transcribe in English.",
    "fr": "Conversation en français. Transcrivez fidèlement en français.",
    "es": "Conversación en español. Transcribe con fidelidad en español.",
    "de": "Gespräch auf Deutsch. Transkribiere getreu auf Deutsch.",
    "ar": "محادثة باللغة العربية. انسخ الكلام بدقة بالعربية.",
    "pt": "Conversa em português. Transcreva fielmente em português.",
    "sw": "Mazungumzo kwa Kiswahili. Nakili kwa usahihi kwa Kiswahili.",
}


def has_groq_key() -> bool:
    """True when a Groq API key is configured."""
    return bool(GROQ_API_KEY)

#: Target-language codes accepted by POST /tts and POST /asr.
#: Don't hardcode elsewhere — prefer the API's error `details.supported`.
TARGET_LANGUAGES: tuple[str, ...] = (
    "am",
    "ar",
    "de",
    "en",
    "es",
    "fr",
    "ha",
    "ig",
    "lg",
    "pt",
    "rw",
    "sn",
    "sw",
    "tw",
    "wo",
    "xh",
    "yo",
    "zu",
)


def has_api_key() -> bool:
    """True when a YarnGPT API key is configured."""
    return bool(YARNGPT_API_KEY)
