"""Optional ElevenLabs speech bridge for v7.0.1.

Secrets stay in the server-side environment.  Only non-secret playback
preferences are persisted in SQLite and exposed to the browser.  Audio is
generated on demand by default so opening a conversation never spends voice
credits by itself.  v7.0 adds Scribe speech-to-text for voice messages and a
half-duplex companion-call UI while keeping the same server-side key.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from config import VOICE_CONFIG
from models import get_db
from runtime_paths import DATA_DIR


ELEVENLABS_API = "https://api.elevenlabs.io"
VOICE_ARCHIVE_DIR = Path(DATA_DIR) / "voice-archive"
VOICE_GREETING_DIR = Path(DATA_DIR) / "voice-greetings"
ALLOWED_DELIVERY_TAGS = {
    "auto", "softly", "teasing", "laughs", "rushed", "drawn out",
}
KNOWN_AUDIO_EVENTS = {
    "silence", "music", "noise", "background noise", "applause", "clapping",
    "laughter", "laughs", "laughing", "cough", "coughing", "breathing",
    "sigh", "sighing", "door", "footsteps", "wind", "rain", "static",
    "静音", "沉默", "音乐", "噪音", "背景音", "掌声", "笑声", "咳嗽",
    "呼吸", "叹气", "脚步声", "风声", "雨声", "杂音",
}
_EVENT_TOKEN_RE = re.compile(r"\[([^\[\]\n]{1,40})\]")
DEFAULT_SETTINGS = {
    "enabled": bool(VOICE_CONFIG.get("enabled", False)),
    "auto_play": bool(VOICE_CONFIG.get("auto_play", False)),
    "transcript_auto_send": False,
    "call_auto_reply": True,
    "voice_id": str(VOICE_CONFIG.get("voice_id") or ""),
    "model_id": str(VOICE_CONFIG.get("model_id") or "eleven_v3"),
    "language_code": str(VOICE_CONFIG.get("language_code") or "zh"),
    "stability": 0.36,
    "similarity_boost": 0.75,
    "style": 0.72,
    "speed": 1.08,
    "use_speaker_boost": True,
    "delivery_tag": "auto",
    "keyterms": [],
    "greeting_text": "嗯，我在。听得到吗？",
    "native_language": "zh",
    "translation_enabled": True,
    "mood_enabled": True,
    "post_eq": bool(VOICE_CONFIG.get("post_eq", True)),
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = fallback
    return round(max(low, min(high, number)), 3)


class VoiceServiceError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class VoiceService:
    def __init__(self) -> None:
        self._settings: dict[str, Any] | None = None

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """CREATE TABLE IF NOT EXISTS voice_settings_state (
                       id INTEGER PRIMARY KEY CHECK (id = 1),
                       settings_json TEXT NOT NULL,
                       updated_at TEXT NOT NULL
                   );
                   CREATE TABLE IF NOT EXISTS voice_call_sessions (
                       id TEXT PRIMARY KEY,
                       chat_session_id TEXT,
                       status TEXT NOT NULL DEFAULT 'active',
                       started_at TEXT NOT NULL,
                       ended_at TEXT,
                       user_turns INTEGER NOT NULL DEFAULT 0,
                       assistant_turns INTEGER NOT NULL DEFAULT 0,
                       private_mode INTEGER NOT NULL DEFAULT 0,
                       sleep_mode INTEGER NOT NULL DEFAULT 0,
                       audio_clock_ms INTEGER NOT NULL DEFAULT 0,
                       last_heartbeat_at TEXT,
                       route TEXT NOT NULL DEFAULT 'speaker',
                       metadata_json TEXT NOT NULL DEFAULT '{}'
                   );
                   CREATE INDEX IF NOT EXISTS idx_voice_call_chat
                       ON voice_call_sessions(chat_session_id, started_at);
                   CREATE TABLE IF NOT EXISTS voice_call_segments (
                       id TEXT PRIMARY KEY,
                       call_id TEXT NOT NULL,
                       chat_session_id TEXT,
                       role TEXT NOT NULL,
                       transcript TEXT NOT NULL DEFAULT '',
                       translation TEXT NOT NULL DEFAULT '',
                       started_ms INTEGER NOT NULL DEFAULT 0,
                       duration_ms INTEGER NOT NULL DEFAULT 0,
                       acoustic_json TEXT NOT NULL DEFAULT '{}',
                       mood_json TEXT NOT NULL DEFAULT '{}',
                       alignment_json TEXT NOT NULL DEFAULT '{}',
                       audio_path TEXT,
                       media_type TEXT,
                       private_mode INTEGER NOT NULL DEFAULT 0,
                       sleep_mode INTEGER NOT NULL DEFAULT 0,
                       created_at TEXT NOT NULL,
                       FOREIGN KEY(call_id) REFERENCES voice_call_sessions(id)
                   );
                   CREATE INDEX IF NOT EXISTS idx_voice_segments_call
                       ON voice_call_segments(call_id, started_ms, created_at);
                   CREATE TABLE IF NOT EXISTS voice_programs (
                       id TEXT PRIMARY KEY,
                       title TEXT NOT NULL,
                       category TEXT NOT NULL DEFAULT '',
                       description TEXT NOT NULL DEFAULT '',
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL
                   );
                   CREATE TABLE IF NOT EXISTS voice_program_items (
                       program_id TEXT NOT NULL,
                       segment_id TEXT NOT NULL,
                       position INTEGER NOT NULL,
                       PRIMARY KEY(program_id, segment_id),
                       FOREIGN KEY(program_id) REFERENCES voice_programs(id),
                       FOREIGN KEY(segment_id) REFERENCES voice_call_segments(id)
                   );
                   CREATE TABLE IF NOT EXISTS voice_sleep_snapshots (
                       id TEXT PRIMARY KEY,
                       call_id TEXT NOT NULL,
                       sample_clock_ms INTEGER NOT NULL,
                       acoustic_json TEXT NOT NULL DEFAULT '{}',
                       created_at TEXT NOT NULL,
                       FOREIGN KEY(call_id) REFERENCES voice_call_sessions(id)
                   );
                   CREATE TABLE IF NOT EXISTS voice_runtime_events (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       call_id TEXT,
                       event TEXT NOT NULL,
                       detail TEXT NOT NULL DEFAULT '',
                       created_at TEXT NOT NULL
                   );
                """
            )
            # Databases created by v7.0 already have voice_call_sessions.  The
            # CREATE statement above cannot add columns to those installations.
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(voice_call_sessions)").fetchall()
            }
            additions = {
                "private_mode": "INTEGER NOT NULL DEFAULT 0",
                "sleep_mode": "INTEGER NOT NULL DEFAULT 0",
                "audio_clock_ms": "INTEGER NOT NULL DEFAULT 0",
                "last_heartbeat_at": "TEXT",
                "route": "TEXT NOT NULL DEFAULT 'speaker'",
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE voice_call_sessions ADD COLUMN {name} {declaration}"
                    )
        VOICE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        VOICE_GREETING_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def speed_supported(model_id: str) -> bool:
        # ElevenLabs currently documents Speed as unavailable for Eleven v3.
        return str(model_id or "").strip() != "eleven_v3"

    @staticmethod
    def _clean_identifier(value: Any, fallback: str = "", limit: int = 128) -> str:
        text = str(value or "").strip()
        if not text:
            return fallback
        if len(text) > limit or not re.fullmatch(r"[A-Za-z0-9_.~-]+", text):
            return fallback
        return text

    @staticmethod
    def _clean_language(value: Any) -> str:
        text = str(value or "zh").strip().lower()
        return text if re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", text) else "zh"

    def _normalise(self, raw: dict[str, Any]) -> dict[str, Any]:
        settings = {**DEFAULT_SETTINGS, **(raw or {})}
        settings["enabled"] = bool(settings.get("enabled", False))
        settings["auto_play"] = bool(settings.get("auto_play", False))
        settings["transcript_auto_send"] = bool(
            settings.get("transcript_auto_send", False)
        )
        settings["call_auto_reply"] = bool(settings.get("call_auto_reply", True))
        settings["voice_id"] = self._clean_identifier(settings.get("voice_id"), "")
        settings["model_id"] = self._clean_identifier(
            settings.get("model_id"), "eleven_v3", 80
        )
        settings["language_code"] = self._clean_language(settings.get("language_code"))
        settings["stability"] = _clamp(settings.get("stability"), 0, 1, 0.36)
        settings["similarity_boost"] = _clamp(
            settings.get("similarity_boost"), 0, 1, 0.75
        )
        settings["style"] = _clamp(settings.get("style"), 0, 1, 0.72)
        settings["speed"] = _clamp(settings.get("speed"), 0.7, 1.2, 1.08)
        settings["use_speaker_boost"] = bool(settings.get("use_speaker_boost", True))
        delivery = str(settings.get("delivery_tag") or "auto").strip().lower()
        settings["delivery_tag"] = delivery if delivery in ALLOWED_DELIVERY_TAGS else "auto"
        raw_keyterms = settings.get("keyterms") or []
        if isinstance(raw_keyterms, str):
            raw_keyterms = re.split(r"[,，\n]", raw_keyterms)
        if not isinstance(raw_keyterms, list):
            raw_keyterms = []
        keyterms: list[str] = []
        seen: set[str] = set()
        for item in raw_keyterms:
            term = re.sub(r"\s+", " ", str(item or "")).strip()[:50]
            lowered = term.casefold()
            if term and lowered not in seen:
                seen.add(lowered)
                keyterms.append(term)
            if len(keyterms) >= 100:
                break
        settings["keyterms"] = keyterms
        settings["greeting_text"] = str(
            settings.get("greeting_text") or DEFAULT_SETTINGS["greeting_text"]
        ).strip()[:240]
        settings["native_language"] = self._clean_language(
            settings.get("native_language") or "zh"
        )
        settings["translation_enabled"] = bool(
            settings.get("translation_enabled", True)
        )
        settings["mood_enabled"] = bool(settings.get("mood_enabled", True))
        settings["post_eq"] = bool(settings.get("post_eq", True))
        return settings

    def load_settings(self, refresh: bool = False) -> dict[str, Any]:
        self.ensure_schema()
        if self._settings is not None and not refresh:
            return deepcopy(self._settings)
        with get_db() as db:
            row = db.execute(
                "SELECT settings_json FROM voice_settings_state WHERE id=1"
            ).fetchone()
        stored: dict[str, Any] = {}
        if row:
            try:
                payload = json.loads(row["settings_json"] or "{}")
                if isinstance(payload, dict):
                    stored = payload
            except Exception:
                stored = {}
        settings = self._normalise(stored)
        self._settings = settings
        if not row:
            self.save_settings(settings)
        return deepcopy(settings)

    def save_settings(self, settings: dict[str, Any]) -> None:
        normalised = self._normalise(settings)
        self.ensure_schema()
        with get_db() as db:
            db.execute(
                """INSERT INTO voice_settings_state(id, settings_json, updated_at)
                   VALUES(1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     settings_json=excluded.settings_json,
                     updated_at=excluded.updated_at""",
                (json.dumps(normalised, ensure_ascii=False), _utcnow()),
            )
        self._settings = normalised

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        settings = self.load_settings()
        allowed = {
            "enabled", "auto_play", "voice_id", "model_id", "language_code",
            "stability", "similarity_boost", "style", "speed",
            "use_speaker_boost", "delivery_tag", "transcript_auto_send",
            "call_auto_reply",
            "keyterms", "greeting_text", "native_language",
            "translation_enabled", "mood_enabled", "post_eq",
        }
        for key, value in (patch or {}).items():
            if key in allowed:
                settings[key] = value
        self.save_settings(settings)
        return self.state_view()

    def state_view(self) -> dict[str, Any]:
        settings = self.load_settings()
        key_ready = bool(str(VOICE_CONFIG.get("api_key") or "").strip())
        voice_ready = bool(settings.get("voice_id"))
        enabled = bool(settings.get("enabled"))
        return {
            "provider": "ElevenLabs",
            "key_configured": key_ready,
            "voice_configured": voice_ready,
            "ready": bool(enabled and key_ready and voice_ready),
            "tts_ready": bool(enabled and key_ready and voice_ready),
            "stt_ready": bool(enabled and key_ready),
            # Browser speech APIs vary by browser, secure-context state and
            # device. The browser adds its measured capability at runtime.
            "browser_capability_requires_client_check": True,
            "stt_model": str(VOICE_CONFIG.get("stt_model_id") or "scribe_v2"),
            "stt_max_bytes": int(VOICE_CONFIG.get("stt_max_bytes", 25 * 1024 ** 2)),
            "speed_supported": self.speed_supported(str(settings.get("model_id") or "")),
            "max_chars": int(VOICE_CONFIG.get("max_chars", 5000)),
            "timestamps_ready": bool(enabled and key_ready and voice_ready),
            "mood_ready": bool(
                settings.get("mood_enabled")
                and str(VOICE_CONFIG.get("mood_api_key") or "").strip()
            ),
            "translation_ready": bool(
                settings.get("translation_enabled")
                and __import__("api_cost").resolve_auxiliary_route()
            ),
            "archive_ready": True,
            "native_bridge_name": "DaxiguaVoice",
            "settings": settings,
        }

    def resolve_voice_id(self, requested: Any = "") -> str:
        """Apply request > saved setting > environment > default-file precedence."""
        candidate = self._clean_identifier(requested, "")
        if candidate:
            return candidate
        settings = self.load_settings()
        candidate = self._clean_identifier(settings.get("voice_id"), "")
        if candidate:
            return candidate
        candidate = self._clean_identifier(VOICE_CONFIG.get("voice_id"), "")
        if candidate:
            return candidate
        default_file = Path(str(VOICE_CONFIG.get("default_voice_file") or ""))
        if default_file.is_file():
            try:
                candidate = self._clean_identifier(
                    default_file.read_text(encoding="utf-8").strip(), ""
                )
            except OSError:
                candidate = ""
        return candidate

    @staticmethod
    def prepare_text(text: str) -> str:
        value = str(text or "").replace("\r\n", "\n")
        value = re.sub(r"```[\s\S]*?```", "\n（代码块已省略）\n", value)
        value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
        value = re.sub(r"\[([^\]]+)\]\((?:https?://|mailto:)[^)]*\)", r"\1", value)
        value = re.sub(r"https?://\S+", "链接", value)
        value = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", value)
        value = re.sub(r"(?m)^\s*>\s?", "", value)
        value = re.sub(r"(?m)^\s*[-*+]\s+", "", value)
        value = re.sub(r"(?m)^\s*\d+[.)]\s+", "", value)
        value = re.sub(r"</?[^>]+>", "", value)
        value = value.replace("**", "").replace("__", "").replace("`", "")
        value = re.sub(r"(?m)^\s*\|?\s*:?-{3,}:?[^\n]*$", "", value)
        value = value.replace("|", "，")
        # A tiny spoken-copy-only pronunciation wash.  The visible chat text
        # remains untouched; only symbols that TTS engines often read awkwardly
        # are normalised here.
        value = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"百分之\1", value)
        value = value.replace("&", "和").replace(" + ", " 加 ")
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def request_payload(self, text: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self._normalise(settings or self.load_settings())
        spoken = self.prepare_text(text)
        delivery = current.get("delivery_tag")
        if current["model_id"] == "eleven_v3" and delivery != "auto":
            spoken = f"[{delivery}] {spoken}"
        payload: dict[str, Any] = {
            "text": spoken,
            "model_id": current["model_id"],
            "voice_settings": {
                "stability": current["stability"],
                "similarity_boost": current["similarity_boost"],
                "style": current["style"],
                "use_speaker_boost": current["use_speaker_boost"],
            },
        }
        if current["model_id"] != "eleven_multilingual_v2":
            payload["language_code"] = current["language_code"]
        if self.speed_supported(current["model_id"]):
            payload["voice_settings"]["speed"] = current["speed"]
        return payload

    @staticmethod
    def _alignment_duration_ms(alignment: Any) -> int:
        if not isinstance(alignment, dict):
            return 0
        ends = alignment.get("character_end_times_seconds")
        if not isinstance(ends, list) or not ends:
            return 0
        try:
            return max(0, round(float(ends[-1]) * 1000))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _event_name(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

    @classmethod
    def clean_transcript(
        cls, transcript: Any, words: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Remove event-only hallucinations without dropping real short speech."""
        text = str(transcript or "").strip()
        events: list[str] = []
        spoken_tokens: list[str] = []
        for raw in words or []:
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("text") or "").strip()
            kind = str(raw.get("type") or "").strip().casefold()
            if kind in {"audio_event", "event", "sound_event"}:
                if token:
                    events.append(token[:80])
            elif token:
                spoken_tokens.append(token)

        def strip_known_event(match: re.Match[str]) -> str:
            label = cls._event_name(match.group(1))
            if label in KNOWN_AUDIO_EVENTS:
                events.append(match.group(0)[:80])
                return " "
            return match.group(0)

        cleaned = _EVENT_TOKEN_RE.sub(strip_known_event, text)
        cleaned = re.sub(r"\s+([，。！？,.!?])", r"\1", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip(" \n,，。")
        if not cleaned and spoken_tokens:
            cleaned = "".join(spoken_tokens).strip()
        return {
            "text": cleaned,
            "audio_events": events[:50],
            "speech_detected": bool(cleaned),
            "raw_text": text,
        }

    @staticmethod
    def _safe_json(value: Any, *, max_chars: int = 16000) -> str:
        try:
            encoded = json.dumps(value if isinstance(value, (dict, list)) else {}, ensure_ascii=False)
        except (TypeError, ValueError):
            encoded = "{}"
        return encoded[:max_chars]

    @staticmethod
    def _upstream_error(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("status") or "")
        if detail:
            return str(detail)
        return str(payload.get("message") or "") if isinstance(payload, dict) else ""

    async def list_voices(self) -> list[dict[str, Any]]:
        key = str(VOICE_CONFIG.get("api_key") or "").strip()
        if not key:
            raise VoiceServiceError("还没有配置 ELEVENLABS_API_KEY", 409)
        timeout = httpx.Timeout(float(VOICE_CONFIG.get("timeout_seconds", 90)))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(
                f"{ELEVENLABS_API}/v2/voices",
                params={"page_size": 100, "sort": "name", "sort_direction": "asc", "include_total_count": "false"},
                headers={"xi-api-key": key, "Accept": "application/json"},
            )
        if response.status_code >= 400:
            note = self._upstream_error(response)
            raise VoiceServiceError(
                f"ElevenLabs 音色读取失败（HTTP {response.status_code}）{f'：{note}' if note else ''}",
                502,
            )
        payload = response.json()
        result = []
        for raw in payload.get("voices", []):
            voice_id = self._clean_identifier(raw.get("voice_id"), "")
            if not voice_id:
                continue
            result.append({
                "voice_id": voice_id,
                "name": str(raw.get("name") or voice_id)[:120],
                "category": str(raw.get("category") or "")[:40],
                "labels": raw.get("labels") if isinstance(raw.get("labels"), dict) else {},
            })
        return result

    async def _post_process_audio(self, audio: bytes, media_type: str) -> bytes:
        settings = self.load_settings()
        if not audio or not settings.get("post_eq") or not VOICE_CONFIG.get("post_eq", True):
            return audio
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return audio
        suffix = ".mp3" if "mpeg" in str(media_type) else ".audio"

        def run_filter() -> bytes:
            source_path = ""
            output_path = ""
            try:
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as source:
                    source.write(audio)
                    source_path = source.name
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as output:
                    output_path = output.name
                command = [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", source_path,
                    "-af",
                    "highpass=f=70,equalizer=f=180:t=q:w=1:g=1.4,"
                    "equalizer=f=3200:t=q:w=1:g=1.0,alimiter=limit=0.95",
                    "-codec:a", "libmp3lame", "-b:a", "128k", output_path,
                ]
                completed = subprocess.run(
                    command, capture_output=True, check=False, timeout=30
                )
                if completed.returncode == 0 and os.path.getsize(output_path) > 0:
                    return Path(output_path).read_bytes()
            except (OSError, subprocess.SubprocessError):
                pass
            finally:
                for path in (source_path, output_path):
                    if path:
                        try:
                            os.unlink(path)
                        except OSError:
                            pass
            return audio

        return await asyncio.to_thread(run_filter)

    async def synthesize(
        self, text: str, *, voice_id: str = ""
    ) -> tuple[bytes, str]:
        key = str(VOICE_CONFIG.get("api_key") or "").strip()
        settings = self.load_settings()
        if not settings.get("enabled"):
            raise VoiceServiceError("语音通道还没有开启", 409)
        if not key:
            raise VoiceServiceError("还没有配置 ELEVENLABS_API_KEY", 409)
        voice_id = self.resolve_voice_id(voice_id)
        if not voice_id:
            raise VoiceServiceError("还没有选择 ElevenLabs 音色", 409)
        payload = self.request_payload(text, settings)
        spoken = str(payload.get("text") or "")
        if not spoken:
            raise VoiceServiceError("这条消息没有可朗读的文字")
        max_chars = max(100, min(int(VOICE_CONFIG.get("max_chars", 5000)), 10000))
        if len(spoken) > max_chars:
            raise VoiceServiceError(
                f"这条回复整理后有 {len(spoken)} 字，超过单次语音上限 {max_chars} 字；请选中较短内容或先让模型简短重述。"
            )
        output_format = self._clean_identifier(
            VOICE_CONFIG.get("output_format"), "mp3_44100_128", 80
        )
        url = f"{ELEVENLABS_API}/v1/text-to-speech/{quote(voice_id, safe='')}"
        timeout = httpx.Timeout(float(VOICE_CONFIG.get("timeout_seconds", 90)))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                url,
                params={"output_format": output_format},
                headers={
                    "xi-api-key": key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json=payload,
            )
        if response.status_code >= 400:
            note = self._upstream_error(response)
            raise VoiceServiceError(
                f"ElevenLabs 生成失败（HTTP {response.status_code}）{f'：{note}' if note else ''}",
                502,
            )
        media_type = response.headers.get("content-type", "audio/mpeg").split(";", 1)[0]
        audio = await self._post_process_audio(response.content, media_type)
        return audio, media_type

    async def synthesize_with_timestamps(
        self, text: str, *, voice_id: str = ""
    ) -> dict[str, Any]:
        key = str(VOICE_CONFIG.get("api_key") or "").strip()
        settings = self.load_settings()
        if not settings.get("enabled"):
            raise VoiceServiceError("语音通道还没有开启", 409)
        if not key:
            raise VoiceServiceError("还没有配置 ELEVENLABS_API_KEY", 409)
        resolved_voice = self.resolve_voice_id(voice_id)
        if not resolved_voice:
            raise VoiceServiceError("还没有选择 ElevenLabs 音色", 409)
        payload = self.request_payload(text, settings)
        spoken = str(payload.get("text") or "")
        if not spoken:
            raise VoiceServiceError("这条消息没有可朗读的文字")
        max_chars = max(100, min(int(VOICE_CONFIG.get("max_chars", 5000)), 10000))
        if len(spoken) > max_chars:
            raise VoiceServiceError(
                f"这条回复整理后有 {len(spoken)} 字，超过单次语音上限 {max_chars} 字"
            )
        output_format = self._clean_identifier(
            VOICE_CONFIG.get("output_format"), "mp3_44100_128", 80
        )
        url = (
            f"{ELEVENLABS_API}/v1/text-to-speech/"
            f"{quote(resolved_voice, safe='')}/with-timestamps"
        )
        timeout = httpx.Timeout(float(VOICE_CONFIG.get("timeout_seconds", 90)))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                url,
                params={"output_format": output_format},
                headers={
                    "xi-api-key": key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
            )
        if response.status_code >= 400:
            note = self._upstream_error(response)
            raise VoiceServiceError(
                f"ElevenLabs 时间戳语音生成失败（HTTP {response.status_code}）"
                f"{f'：{note}' if note else ''}",
                502,
            )
        data = response.json()
        try:
            audio = base64.b64decode(str(data.get("audio_base64") or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise VoiceServiceError("ElevenLabs 返回了无效的音频数据", 502) from exc
        if not audio:
            raise VoiceServiceError("ElevenLabs 没有返回音频", 502)
        audio = await self._post_process_audio(audio, "audio/mpeg")
        alignment = data.get("alignment") if isinstance(data.get("alignment"), dict) else {}
        normalized = (
            data.get("normalized_alignment")
            if isinstance(data.get("normalized_alignment"), dict) else {}
        )
        return {
            "audio": audio,
            "media_type": "audio/mpeg",
            "alignment": alignment,
            "normalized_alignment": normalized,
            "duration_ms": self._alignment_duration_ms(alignment or normalized),
            "spoken_text": spoken,
            "voice_id": resolved_voice,
        }

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "voice-message.webm",
        media_type: str = "audio/webm",
        language_code: str | None = None,
        keyterms: list[str] | None = None,
    ) -> dict[str, Any]:
        """Transcribe one same-origin voice upload with ElevenLabs Scribe."""
        key = str(VOICE_CONFIG.get("api_key") or "").strip()
        settings = self.load_settings()
        if not settings.get("enabled"):
            raise VoiceServiceError("语音通道还没有开启", 409)
        if not key:
            raise VoiceServiceError("还没有配置 ELEVENLABS_API_KEY", 409)
        if not audio:
            raise VoiceServiceError("没有收到录音内容")
        max_bytes = max(
            1024,
            min(int(VOICE_CONFIG.get("stt_max_bytes", 25 * 1024 ** 2)), 256 * 1024 ** 2),
        )
        if len(audio) > max_bytes:
            raise VoiceServiceError(
                f"这段录音有 {len(audio) / 1024 / 1024:.1f} MB，超过本机设置的 "
                f"{max_bytes / 1024 / 1024:.0f} MB 上限",
                413,
            )
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(filename or ""))[:120]
        if not safe_name:
            safe_name = "voice-message.webm"
        safe_media = str(media_type or "application/octet-stream").split(";", 1)[0]
        if not re.fullmatch(r"(?:audio|video)/[A-Za-z0-9.+-]+", safe_media):
            safe_media = "application/octet-stream"
        model_id = self._clean_identifier(
            VOICE_CONFIG.get("stt_model_id"), "scribe_v2", 80
        )
        language = self._clean_language(
            language_code or settings.get("language_code") or "zh"
        )
        form: list[tuple[str, str]] = [
            ("model_id", model_id),
            ("language_code", language),
            (
                "tag_audio_events",
                "true" if VOICE_CONFIG.get("stt_tag_audio_events", True) else "false",
            ),
            ("timestamps_granularity", "word"),
            ("diarize", "false"),
        ]
        raw_keyterms = keyterms if isinstance(keyterms, list) else settings.get("keyterms", [])
        seen_keyterms: set[str] = set()
        for raw in raw_keyterms or []:
            term = re.sub(r"\s+", " ", str(raw or "")).strip()[:50]
            lowered = term.casefold()
            if term and lowered not in seen_keyterms:
                seen_keyterms.add(lowered)
                form.append(("keyterms", term))
            if len(seen_keyterms) >= 100:
                break
        timeout = httpx.Timeout(float(VOICE_CONFIG.get("timeout_seconds", 90)))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                f"{ELEVENLABS_API}/v1/speech-to-text",
                headers={"xi-api-key": key, "Accept": "application/json"},
                data=form,
                files={"file": (safe_name, audio, safe_media)},
            )
        if response.status_code >= 400:
            note = self._upstream_error(response)
            raise VoiceServiceError(
                f"ElevenLabs 转写失败（HTTP {response.status_code}）"
                f"{f'：{note}' if note else ''}",
                502,
            )
        payload = response.json()
        transcript = str(payload.get("text") or "").strip()
        words = []
        for raw in payload.get("words", [])[:5000]:
            if not isinstance(raw, dict):
                continue
            words.append({
                "text": str(raw.get("text") or "")[:80],
                "start": raw.get("start"),
                "end": raw.get("end"),
                "type": str(raw.get("type") or "")[:24],
                "speaker_id": str(raw.get("speaker_id") or "")[:40],
            })
        cleaned = self.clean_transcript(transcript, words)
        return {
            "text": cleaned["text"],
            "raw_text": transcript,
            "speech_detected": cleaned["speech_detected"],
            "audio_events": cleaned["audio_events"],
            "language_code": str(payload.get("language_code") or language)[:16],
            "language_probability": payload.get("language_probability"),
            "words": words,
            "provider": "ElevenLabs",
            "model": model_id,
        }

    @staticmethod
    def _json_object_from_text(value: Any) -> dict[str, Any]:
        if isinstance(value, list):
            chunks = []
            for item in value:
                if isinstance(item, dict):
                    chunks.append(str(item.get("text") or item.get("content") or ""))
            value = "\n".join(chunks)
        text = str(value or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                return {}
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}

    @staticmethod
    def sanitize_acoustic(value: Any) -> dict[str, Any]:
        raw = value if isinstance(value, dict) else {}

        def number(name: str, low: float, high: float) -> float:
            try:
                parsed = float(raw.get(name) or 0)
            except (TypeError, ValueError):
                parsed = 0
            return round(max(low, min(high, parsed)), 4)

        return {
            "rms": number("rms", 0, 1),
            "peak": number("peak", 0, 1),
            "pitch_hz": number("pitch_hz", 0, 1200),
            "pitch_min_hz": number("pitch_min_hz", 0, 1200),
            "pitch_max_hz": number("pitch_max_hz", 0, 1200),
            "voiced_ratio": number("voiced_ratio", 0, 1),
            "frame_count": int(number("frame_count", 0, 1000000)),
            "duration_ms": int(number("duration_ms", 0, 60 * 60 * 1000)),
        }

    @staticmethod
    def _local_mood_hint(acoustic: dict[str, Any]) -> dict[str, Any]:
        rms = float(acoustic.get("rms") or 0)
        pitch = float(acoustic.get("pitch_hz") or 0)
        voiced = float(acoustic.get("voiced_ratio") or 0)
        if voiced < 0.04:
            emotion, pace = "无法判断", "没有稳定人声"
        elif rms > 0.13 or pitch > 330:
            emotion, pace = "声音能量较高", "可能偏快或偏激动"
        elif rms < 0.025:
            emotion, pace = "声音很轻", "可能偏慢或疲惫"
        else:
            emotion, pace = "声音平稳", "节奏普通"
        return {
            "provider": "local-acoustic",
            "available": bool(voiced >= 0.04),
            "speech": bool(voiced >= 0.04),
            "emotion": emotion,
            "intensity": round(min(1.0, max(0.0, rms * 4)), 2),
            "pace": pace,
            "background": "仅根据本机声学量判断，未调用情绪模型",
            "confidence": 0.2,
        }

    async def analyze_mood(
        self,
        audio: bytes,
        *,
        media_type: str = "audio/webm",
        acoustic: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sanitized = self.sanitize_acoustic(acoustic)
        settings = self.load_settings()
        key = str(VOICE_CONFIG.get("mood_api_key") or "").strip()
        if not settings.get("mood_enabled") or not key or not audio:
            return self._local_mood_hint(sanitized)
        if len(audio) > int(VOICE_CONFIG.get("mood_max_bytes", 8 * 1024 * 1024)):
            result = self._local_mood_hint(sanitized)
            result["detail"] = "录音超过情绪分析大小上限"
            return result
        converted_audio, converted_media = await asyncio.to_thread(
            self._archive_as_mp3, audio, media_type
        )
        clean_media = str(converted_media or media_type).split(";", 1)[0].lower()
        audio_format = {
            "audio/mpeg": "mp3",
            "audio/mp3": "mp3",
            "audio/wav": "wav",
            "audio/x-wav": "wav",
            "audio/mp4": "m4a",
            "audio/x-m4a": "m4a",
            "audio/ogg": "ogg",
            "audio/webm": "webm",
        }.get(clean_media, "mp3")
        data_uri = (
            f"data:{clean_media};base64,"
            f"{base64.b64encode(converted_audio).decode('ascii')}"
        )
        prompt = (
            "只分析这段录音中可听见的人声情绪与背景，不转录内容。"
            "只返回一个 JSON 对象，字段严格为：speech(boolean)、emotion(中文短语)、"
            "intensity(0到1数字)、pace(中文短语)、background(中文短语)、confidence(0到1数字)。"
            "听不到真人说话时 speech=false，不要猜测。"
        )
        payload = {
            "model": str(VOICE_CONFIG.get("mood_model") or "qwen3.5-omni-flash"),
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": data_uri, "format": audio_format},
                    },
                ],
            }],
            "temperature": 0,
            "enable_thinking": False,
            "stream": False,
        }
        base_url = str(
            VOICE_CONFIG.get("mood_base_url")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).rstrip("/")
        timeout = httpx.Timeout(float(VOICE_CONFIG.get("mood_timeout_seconds", 45)))
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if response.status_code >= 400:
                raise VoiceServiceError(
                    f"Qwen 音频情绪分析失败（HTTP {response.status_code}）", 502
                )
            body = response.json()
            content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = self._json_object_from_text(content)
            if not parsed:
                raise VoiceServiceError("Qwen 音频情绪分析没有返回有效 JSON", 502)
            return {
                "provider": "Qwen Omni",
                "available": True,
                "speech": bool(parsed.get("speech", True)),
                "emotion": str(parsed.get("emotion") or "无法判断")[:80],
                "intensity": _clamp(parsed.get("intensity"), 0, 1, 0.5),
                "pace": str(parsed.get("pace") or "无法判断")[:80],
                "background": str(parsed.get("background") or "未描述")[:160],
                "confidence": _clamp(parsed.get("confidence"), 0, 1, 0.5),
            }
        except (httpx.HTTPError, VoiceServiceError, ValueError, KeyError) as exc:
            result = self._local_mood_hint(sanitized)
            result["detail"] = str(exc)[:240]
            return result

    @staticmethod
    def _needs_translation(text: str, target_language: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or ""))
        if not compact:
            return False
        if target_language.startswith("zh"):
            chinese = len(re.findall(r"[\u3400-\u9fff]", compact))
            letters = len(re.findall(r"[A-Za-z\u3400-\u9fff]", compact))
            return letters > 0 and chinese / max(letters, 1) < 0.55
        return True

    async def translate_lines(
        self, lines: list[str], *, target_language: str = "zh"
    ) -> dict[str, Any]:
        """Translate transcript lines via the DeepSeek Flash mechanical route."""
        from api_cost import auxiliary_chat, resolve_auxiliary_route

        cleaned = [str(line or "").strip()[:2000] for line in lines[:80]]
        result_lines = list(cleaned)
        indices = [
            index for index, line in enumerate(cleaned)
            if self._needs_translation(line, target_language)
        ]
        if not indices:
            return {"provider": "none", "translated": False, "lines": result_lines}
        settings = self.load_settings()
        route = resolve_auxiliary_route()
        if not settings.get("translation_enabled") or not route:
            return {
                "provider": "unavailable",
                "translated": False,
                "lines": result_lines,
                "missing_key": True,
            }
        numbered = "\n".join(
            f"{position + 1}\t{cleaned[index]}"
            for position, index in enumerate(indices)
        )
        try:
            result = await auxiliary_chat(
                messages=[{"role": "user", "content": numbered}],
                purpose="voice_translation",
                system_prompt=(
                    f"把每一行翻译为 {target_language}。保持原编号和制表符；"
                    "一行输入必须对应一行输出，不解释，不合并，不遗漏。"
                ),
                max_output_tokens=8192,
            )
            if not result:
                raise VoiceServiceError("DeepSeek Flash 逐行翻译通道不可用", 502)
            content = str(result.get("content") or "")
            mapped: dict[int, str] = {}
            for raw in content.splitlines():
                match = re.match(r"^\s*(\d+)\s*[\t:：]\s*(.+?)\s*$", raw)
                if match:
                    mapped[int(match.group(1))] = match.group(2).strip()
            if len(mapped) != len(indices):
                raise VoiceServiceError("逐行翻译返回的编号数量不一致", 502)
            for position, original_index in enumerate(indices, start=1):
                result_lines[original_index] = mapped[position]
            return {
                "provider": f"{route[0]} · {route[1]}",
                "translated": True,
                "lines": result_lines,
                "translated_indices": indices,
            }
        except Exception as exc:
            return {
                "provider": f"{route[0]} · {route[1]}",
                "translated": False,
                "lines": result_lines,
                "error": str(exc)[:240],
            }

    @staticmethod
    def _call_public(row: Any) -> dict[str, Any] | None:
        if not row:
            return None
        item = dict(row)
        item["private_mode"] = bool(item.get("private_mode"))
        item["sleep_mode"] = bool(item.get("sleep_mode"))
        try:
            item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
        except (TypeError, ValueError):
            item["metadata"] = {}
        return item

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM voice_call_sessions WHERE id = ?",
                (str(call_id or "")[:64],),
            ).fetchone()
        return self._call_public(row)

    def start_call(
        self,
        chat_session_id: str | None,
        *,
        private_mode: bool = False,
        sleep_mode: bool = False,
    ) -> dict[str, Any]:
        self.ensure_schema()
        call_id = uuid.uuid4().hex
        now = _utcnow()
        with get_db() as db:
            db.execute(
                """INSERT INTO voice_call_sessions
                   (id, chat_session_id, status, started_at, private_mode,
                    sleep_mode, last_heartbeat_at)
                   VALUES (?, ?, 'active', ?, ?, ?, ?)""",
                (
                    call_id,
                    str(chat_session_id or "")[:128],
                    now,
                    int(bool(private_mode)),
                    int(bool(sleep_mode)),
                    now,
                ),
            )
        return self.get_call(call_id) or {
            "id": call_id, "status": "active", "started_at": now
        }

    def update_call(self, call_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_call(call_id)
        if not current:
            return None
        updates: list[str] = []
        values: list[Any] = []
        if "private_mode" in patch:
            updates.append("private_mode = ?")
            values.append(int(bool(patch.get("private_mode"))))
        if "sleep_mode" in patch:
            updates.append("sleep_mode = ?")
            values.append(int(bool(patch.get("sleep_mode"))))
        if "route" in patch:
            route = str(patch.get("route") or "speaker").strip().lower()
            updates.append("route = ?")
            values.append(route if route in {"speaker", "earpiece"} else "speaker")
        if "audio_clock_ms" in patch:
            try:
                clock = int(patch.get("audio_clock_ms") or 0)
            except (TypeError, ValueError):
                clock = 0
            updates.append("audio_clock_ms = MAX(audio_clock_ms, ?)")
            values.append(max(0, min(clock, 7 * 24 * 60 * 60 * 1000)))
        if not updates:
            return current
        values.append(str(call_id or "")[:64])
        with get_db() as db:
            db.execute(
                f"UPDATE voice_call_sessions SET {', '.join(updates)} "
                "WHERE id = ? AND status = 'active'",
                tuple(values),
            )
        return self.get_call(call_id)

    def heartbeat(self, call_id: str, audio_clock_ms: int = 0) -> dict[str, Any] | None:
        now = _utcnow()
        with get_db() as db:
            db.execute(
                """UPDATE voice_call_sessions
                   SET last_heartbeat_at = ?, audio_clock_ms = MAX(audio_clock_ms, ?)
                   WHERE id = ? AND status = 'active'""",
                (
                    now,
                    max(0, min(int(audio_clock_ms or 0), 7 * 24 * 60 * 60 * 1000)),
                    str(call_id or "")[:64],
                ),
            )
        return self.get_call(call_id)

    def note_call_turn(self, call_id: str, role: str) -> bool:
        column = "user_turns" if role == "user" else "assistant_turns"
        with get_db() as db:
            cursor = db.execute(
                f"""UPDATE voice_call_sessions
                    SET {column} = {column} + 1
                    WHERE id = ? AND status = 'active'""",
                (str(call_id or "")[:64],),
            )
        return bool(cursor.rowcount)

    def end_call(self, call_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        now = _utcnow()
        with get_db() as db:
            db.execute(
                """UPDATE voice_call_sessions
                   SET status = 'ended', ended_at = ?
                   WHERE id = ? AND status = 'active'""",
                (now, str(call_id or "")[:64]),
            )
            row = db.execute(
                "SELECT * FROM voice_call_sessions WHERE id = ?",
                (str(call_id or "")[:64],),
            ).fetchone()
        return self._call_public(row)

    @staticmethod
    def _extension_for_media_type(media_type: str) -> str:
        value = str(media_type or "").lower()
        if "mpeg" in value or "mp3" in value:
            return ".mp3"
        if "mp4" in value or "m4a" in value:
            return ".m4a"
        if "ogg" in value:
            return ".ogg"
        if "wav" in value:
            return ".wav"
        return ".webm"

    @staticmethod
    def _archive_as_mp3(audio: bytes, media_type: str) -> tuple[bytes, str]:
        if not audio or "mpeg" in str(media_type).lower() or "mp3" in str(media_type).lower():
            return audio, "audio/mpeg"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return audio, media_type
        source_path = ""
        output_path = ""
        try:
            suffix = VoiceService._extension_for_media_type(media_type)
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as source:
                source.write(audio)
                source_path = source.name
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as output:
                output_path = output.name
            completed = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", source_path, "-vn", "-codec:a", "libmp3lame",
                    "-b:a", "128k", output_path,
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
            if completed.returncode == 0 and os.path.getsize(output_path) > 0:
                return Path(output_path).read_bytes(), "audio/mpeg"
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            for path in (source_path, output_path):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        return audio, media_type

    def record_segment(
        self,
        call_id: str,
        *,
        role: str,
        transcript: str = "",
        translation: str = "",
        started_ms: int = 0,
        duration_ms: int = 0,
        acoustic: dict[str, Any] | None = None,
        mood: dict[str, Any] | None = None,
        alignment: dict[str, Any] | None = None,
        audio: bytes = b"",
        media_type: str = "audio/webm",
        private_mode: bool | None = None,
        sleep_mode: bool | None = None,
    ) -> dict[str, Any]:
        call = self.get_call(call_id)
        if not call:
            raise VoiceServiceError("通话记录不存在", 404)
        if role not in {"user", "assistant"}:
            raise VoiceServiceError("语音片段角色无效")
        private = bool(call.get("private_mode")) if private_mode is None else bool(private_mode)
        sleep = bool(call.get("sleep_mode")) if sleep_mode is None else bool(sleep_mode)
        segment_id = uuid.uuid4().hex
        safe_media = str(media_type or "application/octet-stream").split(";", 1)[0][:80]
        audio_name: str | None = None
        if audio and not private and not sleep:
            audio, safe_media = self._archive_as_mp3(audio, safe_media)
            extension = self._extension_for_media_type(safe_media)
            audio_name = f"{segment_id}{extension}"
            target = VOICE_ARCHIVE_DIR / audio_name
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(audio)
            os.replace(temporary, target)
        started = max(0, min(int(started_ms or 0), 7 * 24 * 60 * 60 * 1000))
        duration = max(0, min(int(duration_ms or 0), 60 * 60 * 1000))
        with get_db() as db:
            db.execute(
                """INSERT INTO voice_call_segments
                   (id, call_id, chat_session_id, role, transcript, translation,
                    started_ms, duration_ms, acoustic_json, mood_json,
                    alignment_json, audio_path, media_type, private_mode,
                    sleep_mode, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    segment_id,
                    str(call_id)[:64],
                    str(call.get("chat_session_id") or "")[:128],
                    role,
                    str(transcript or "")[:20000],
                    str(translation or "")[:20000],
                    started,
                    duration,
                    self._safe_json(self.sanitize_acoustic(acoustic)),
                    self._safe_json(mood),
                    self._safe_json(alignment, max_chars=100000),
                    audio_name,
                    safe_media,
                    int(private),
                    int(sleep),
                    _utcnow(),
                ),
            )
            db.execute(
                """UPDATE voice_call_sessions
                   SET audio_clock_ms = MAX(audio_clock_ms, ?)
                   WHERE id = ?""",
                (started + duration, str(call_id)[:64]),
            )
        return self.get_segment(segment_id) or {"id": segment_id}

    @staticmethod
    def _segment_public(row: Any) -> dict[str, Any] | None:
        if not row:
            return None
        item = dict(row)
        for source, target in (
            ("acoustic_json", "acoustic"),
            ("mood_json", "mood"),
            ("alignment_json", "alignment"),
        ):
            try:
                item[target] = json.loads(item.pop(source, "{}") or "{}")
            except (TypeError, ValueError):
                item[target] = {}
        item["private_mode"] = bool(item.get("private_mode"))
        item["sleep_mode"] = bool(item.get("sleep_mode"))
        item["audio_url"] = (
            f"/api/voice/archive/{item['id']}/audio" if item.get("audio_path") else ""
        )
        item.pop("audio_path", None)
        return item

    def get_segment(self, segment_id: str) -> dict[str, Any] | None:
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM voice_call_segments WHERE id = ?",
                (str(segment_id or "")[:64],),
            ).fetchone()
        return self._segment_public(row)

    def segment_audio(self, segment_id: str) -> tuple[Path, str] | None:
        with get_db() as db:
            row = db.execute(
                "SELECT audio_path, media_type FROM voice_call_segments WHERE id = ?",
                (str(segment_id or "")[:64],),
            ).fetchone()
        if not row or not row["audio_path"]:
            return None
        candidate = (VOICE_ARCHIVE_DIR / str(row["audio_path"])).resolve()
        if VOICE_ARCHIVE_DIR.resolve() not in candidate.parents or not candidate.is_file():
            return None
        return candidate, str(row["media_type"] or "audio/mpeg")

    def list_segments(
        self, *, call_id: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        clauses = ["1=1"]
        values: list[Any] = []
        if call_id:
            clauses.append("call_id = ?")
            values.append(str(call_id)[:64])
        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))
        where = " AND ".join(clauses)
        with get_db() as db:
            rows = db.execute(
                f"""SELECT * FROM voice_call_segments WHERE {where}
                    ORDER BY created_at DESC, started_ms DESC LIMIT ? OFFSET ?""",
                (*values, safe_limit, safe_offset),
            ).fetchall()
            count = db.execute(
                f"SELECT COUNT(*) AS count FROM voice_call_segments WHERE {where}",
                tuple(values),
            ).fetchone()["count"]
        return {
            "items": [self._segment_public(row) for row in rows],
            "total": int(count or 0),
            "limit": safe_limit,
            "offset": safe_offset,
        }

    def create_program(
        self,
        title: str,
        segment_ids: list[str],
        *,
        category: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        clean_title = re.sub(r"\s+", " ", str(title or "")).strip()[:120]
        if not clean_title:
            raise VoiceServiceError("节目名称不能为空")
        clean_ids: list[str] = []
        seen: set[str] = set()
        for raw in segment_ids[:500]:
            value = str(raw or "")[:64]
            if value and value not in seen:
                seen.add(value)
                clean_ids.append(value)
        if not clean_ids:
            raise VoiceServiceError("至少选择一段有录音的通话片段")
        placeholders = ",".join("?" for _ in clean_ids)
        with get_db() as db:
            existing = {
                str(row["id"])
                for row in db.execute(
                    f"SELECT id FROM voice_call_segments WHERE id IN ({placeholders}) "
                    "AND audio_path IS NOT NULL",
                    tuple(clean_ids),
                ).fetchall()
            }
            ordered = [item for item in clean_ids if item in existing]
            if not ordered:
                raise VoiceServiceError("所选片段没有可播放的存档音频")
            program_id = uuid.uuid4().hex
            now = _utcnow()
            db.execute(
                """INSERT INTO voice_programs
                   (id, title, category, description, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    program_id, clean_title, str(category or "")[:80],
                    str(description or "")[:1000], now, now,
                ),
            )
            db.executemany(
                """INSERT INTO voice_program_items(program_id, segment_id, position)
                   VALUES (?, ?, ?)""",
                [(program_id, segment_id, index) for index, segment_id in enumerate(ordered)],
            )
        return self.get_program(program_id) or {"id": program_id, "title": clean_title}

    def get_program(self, program_id: str) -> dict[str, Any] | None:
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM voice_programs WHERE id = ?",
                (str(program_id or "")[:64],),
            ).fetchone()
            if not row:
                return None
            segments = db.execute(
                """SELECT s.* FROM voice_program_items i
                   JOIN voice_call_segments s ON s.id = i.segment_id
                   WHERE i.program_id = ? ORDER BY i.position""",
                (str(program_id or "")[:64],),
            ).fetchall()
        item = dict(row)
        item["items"] = [self._segment_public(segment) for segment in segments]
        return item

    def list_programs(self, limit: int = 100) -> list[dict[str, Any]]:
        with get_db() as db:
            rows = db.execute(
                """SELECT p.*, COUNT(i.segment_id) AS item_count
                   FROM voice_programs p
                   LEFT JOIN voice_program_items i ON i.program_id = p.id
                   GROUP BY p.id ORDER BY p.updated_at DESC LIMIT ?""",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_sleep_snapshot(
        self, call_id: str, sample_clock_ms: int, acoustic: dict[str, Any]
    ) -> dict[str, Any]:
        if not self.get_call(call_id):
            raise VoiceServiceError("通话记录不存在", 404)
        item = {
            "id": uuid.uuid4().hex,
            "call_id": str(call_id)[:64],
            "sample_clock_ms": max(0, int(sample_clock_ms or 0)),
            "acoustic": self.sanitize_acoustic(acoustic),
            "created_at": _utcnow(),
        }
        with get_db() as db:
            db.execute(
                """INSERT INTO voice_sleep_snapshots
                   (id, call_id, sample_clock_ms, acoustic_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    item["id"], item["call_id"], item["sample_clock_ms"],
                    self._safe_json(item["acoustic"]), item["created_at"],
                ),
            )
        return item

    def log_event(self, call_id: str, event: str, detail: str = "") -> None:
        name = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(event or ""))[:80]
        if not name:
            return
        with get_db() as db:
            db.execute(
                """INSERT INTO voice_runtime_events(call_id, event, detail, created_at)
                   VALUES (?, ?, ?, ?)""",
                (str(call_id or "")[:64], name, str(detail or "")[:1000], _utcnow()),
            )

    def _greeting_cache_path(self) -> Path:
        settings = self.load_settings()
        voice_id = self.resolve_voice_id()
        digest = hashlib.sha256(
            json.dumps(
                {
                    "voice_id": voice_id,
                    "model": settings.get("model_id"),
                    "text": settings.get("greeting_text"),
                    "delivery": settings.get("delivery_tag"),
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:20]
        return VOICE_GREETING_DIR / f"greeting-{digest}.mp3"

    def current_greeting(self) -> Path | None:
        path = self._greeting_cache_path()
        return path if path.is_file() and path.stat().st_size > 0 else None

    async def generate_greeting(self) -> Path:
        path = self._greeting_cache_path()
        if path.is_file() and path.stat().st_size > 0:
            return path
        text = str(self.load_settings().get("greeting_text") or "嗯，我在。")
        audio, _ = await self.synthesize(text)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(audio)
        os.replace(temporary, path)
        return path

    def close_stale_calls(self, max_age_hours: int = 12) -> int:
        """Recover calls left active when Safari suspended or killed the page."""
        self.ensure_schema()
        hours = max(1, min(int(max_age_hours), 168))
        now = _utcnow()
        with get_db() as db:
            cursor = db.execute(
                """UPDATE voice_call_sessions
                   SET status='interrupted', ended_at=?
                   WHERE status='active'
                     AND (
                       julianday(COALESCE(last_heartbeat_at, started_at)) < julianday('now', ?)
                       OR julianday(started_at) < julianday('now', ?)
                     )""",
                (now, "-150 seconds", f"-{hours} hours"),
            )
            return max(0, int(cursor.rowcount or 0))

    def health(self) -> dict[str, Any]:
        state = self.state_view()
        if not state["key_configured"]:
            return {"health": "warn", "detail": "可选语音未配置 Key；文字聊天不受影响"}
        if not state["voice_configured"]:
            return {
                "health": "warn",
                "detail": "Scribe 语音转写可用；尚未选择 ElevenLabs 朗读音色",
            }
        return {
            "health": "ok" if state["settings"].get("enabled") else "warn",
            "detail": "语音消息、陪伴通话与按需朗读已就绪"
            if state["settings"].get("enabled")
            else "ElevenLabs 已配置但语音总开关关闭",
        }


voice_service = VoiceService()
