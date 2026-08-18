"""v5.7 附件存储、解析与模型上下文适配。

设计目标：
- 所有附件先安全落盘，再以 ID 引用；聊天数据库不塞大块二进制。
- DeepSeek / GLM 等兼容模型使用本地提取文字。
- OpenAI Responses / Anthropic Messages 预留原生图片、PDF 内容块。
- 解析失败不阻断聊天，只返回清晰状态。
"""
from __future__ import annotations

import base64
import io
import asyncio
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from runtime_paths import DATA_DIR, UPLOAD_DIR
from config import _env_int
from webpage_service import webpage_service
from typing import Any, Iterable

try:
    from PIL import Image, UnidentifiedImageError
except Exception:  # pragma: no cover
    Image = None
    UnidentifiedImageError = OSError
from xml.etree import ElementTree as ET

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover - 依赖未安装时仍允许应用启动并报告状态
    PdfReader = None


UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = _env_int(
    "ATTACHMENT_MAX_BYTES", 25 * 1024 * 1024,
    min_value=1024, max_value=512 * 1024 * 1024,
)
MAX_EXTRACT_CHARS = _env_int(
    "ATTACHMENT_MAX_EXTRACT_CHARS", 120000, min_value=1000, max_value=2000000,
)
MAX_PDF_PAGES = _env_int(
    "ATTACHMENT_MAX_PDF_PAGES", 80, min_value=1, max_value=2000,
)
MAX_NATIVE_BYTES = _env_int(
    "ATTACHMENT_MAX_NATIVE_BYTES", 15 * 1024 * 1024,
    min_value=1024, max_value=100 * 1024 * 1024,
)
MAX_ARCHIVE_UNCOMPRESSED_BYTES = _env_int(
    "ATTACHMENT_MAX_ARCHIVE_UNCOMPRESSED_BYTES", 100 * 1024 * 1024,
    min_value=1024, max_value=2 * 1024 * 1024 * 1024,
)
MAX_ARCHIVE_MEMBERS = _env_int(
    "ATTACHMENT_MAX_ARCHIVE_MEMBERS", 1200, min_value=1, max_value=20000,
)
MAX_ARCHIVE_MEMBER_READ_BYTES = _env_int(
    "ATTACHMENT_MAX_ARCHIVE_MEMBER_READ_BYTES", 2 * 1024 * 1024,
    min_value=1024, max_value=32 * 1024 * 1024,
)
MAX_ARCHIVE_MEMBER_TEXT_CHARS = _env_int(
    "ATTACHMENT_MAX_ARCHIVE_MEMBER_TEXT_CHARS", 12000, min_value=500, max_value=200000,
)
MAX_ARCHIVE_MANIFEST_CHARS = _env_int(
    "ATTACHMENT_MAX_ARCHIVE_MANIFEST_CHARS", 10000, min_value=1000, max_value=200000,
)
MAX_ARCHIVE_LISTED_MEMBERS = _env_int(
    "ATTACHMENT_MAX_ARCHIVE_LISTED_MEMBERS", 500, min_value=10, max_value=5000,
)
MAX_AUDIO_BYTES = _env_int(
    "AUDIO_ATTACHMENT_MAX_BYTES", 150 * 1024 * 1024,
    min_value=1024, max_value=1024 * 1024 * 1024,
)
MAX_VIDEO_BYTES = _env_int(
    "VIDEO_ATTACHMENT_MAX_BYTES", 200 * 1024 * 1024,
    min_value=1024, max_value=2 * 1024 * 1024 * 1024,
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
AUDIO_EXTENSIONS = {
    ".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus",
    ".aac", ".aif", ".aiff", ".caf",
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
ARCHIVE_EXTENSIONS = {".zip"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".jsonl", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".css", ".scss", ".less", ".js", ".jsx", ".ts",
    ".tsx", ".py", ".sh", ".zsh", ".bash", ".sql", ".log", ".ini", ".cfg",
    ".toml", ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".go", ".rs",
    ".swift", ".kt", ".kts", ".rb", ".php", ".vue", ".svelte", ".ipynb",
}
DOCUMENT_EXTENSIONS = {".docx", ".pptx"}
SPREADSHEET_EXTENSIONS = {".xlsx"}
EBOOK_EXTENSIONS = {".epub"}
ALLOWED_EXTENSIONS = (
    IMAGE_EXTENSIONS | TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS
    | SPREADSHEET_EXTENSIONS | EBOOK_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
    | ARCHIVE_EXTENSIONS | {".pdf"}
)
OPENAI_NATIVE_FILE_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx"}

MIME_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg; codecs=opus",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".aac": "audio/aac",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".caf": "audio/x-caf",
    ".zip": "application/zip",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".epub": "application/epub+zip",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".markdown": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
}


def _safe_mime_for_suffix(suffix: str) -> str:
    if suffix in MIME_BY_EXTENSION:
        return MIME_BY_EXTENSION[suffix]
    if suffix in TEXT_EXTENSIONS:
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


def _validate_image(raw: bytes, suffix: str) -> None:
    if Image is None:
        raise ValueError("图片校验组件 Pillow 未安装")
    expected = {
        ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
        ".gif": "GIF", ".webp": "WEBP",
    }[suffix]
    try:
        with Image.open(io.BytesIO(raw)) as image:
            actual = str(image.format or "").upper()
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("图片内容损坏，或与扩展名不符") from exc
    if actual != expected:
        raise ValueError(f"图片内容实际是 {actual or '未知格式'}，与 {suffix} 扩展名不符")


def _validate_pdf(raw: bytes) -> None:
    if not raw.startswith(b"%PDF-"):
        raise ValueError("PDF 文件头无效")
    if PdfReader is None:
        raise ValueError("PDF 校验组件 pypdf 未安装")
    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        _ = len(reader.pages)
    except Exception as exc:
        raise ValueError("PDF 内容损坏或无法解析") from exc


def _validate_audio(raw: bytes, suffix: str) -> None:
    """Validate common audio containers by their real signature.

    Multipart MIME is controlled by the browser, so accepting it would let a
    renamed executable masquerade as a playable attachment.  Full codec
    validation remains Ocean/FFmpeg's job; this gate only proves the container
    matches the allowlisted extension before the bytes are stored.
    """
    valid = False
    if suffix == ".wav":
        valid = len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    elif suffix == ".flac":
        valid = raw.startswith(b"fLaC")
    elif suffix in {".ogg", ".opus"}:
        valid = raw.startswith(b"OggS")
    elif suffix == ".webm":
        valid = raw.startswith(b"\x1a\x45\xdf\xa3")
    elif suffix == ".m4a":
        valid = len(raw) >= 12 and raw[4:8] == b"ftyp"
    elif suffix == ".mp3":
        valid = raw.startswith(b"ID3") or (
            len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0
        )
    elif suffix == ".aac":
        valid = len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xF6) == 0xF0
    elif suffix in {".aif", ".aiff"}:
        valid = (
            len(raw) >= 12 and raw[:4] == b"FORM"
            and raw[8:12] in {b"AIFF", b"AIFC"}
        )
    elif suffix == ".caf":
        valid = raw.startswith(b"caff")
    if not valid:
        raise ValueError(f"音频内容损坏，或与 {suffix} 扩展名不符")



def _validate_video(raw: bytes, suffix: str) -> None:
    """Cheap container signature validation before ffprobe does codec validation."""
    valid = False
    if suffix in {".mp4", ".mov", ".m4v"}:
        valid = len(raw) >= 12 and raw[4:8] == b"ftyp"
    elif suffix in {".mkv", ".webm"}:
        valid = raw.startswith(b"\x1a\x45\xdf\xa3")
    if not valid:
        raise ValueError(f"视频内容损坏，或与 {suffix} 扩展名不符")


def _validate_zip(raw: bytes) -> None:
    """Validate ZIP structure without extracting anything to disk."""
    if not raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        raise ValueError("ZIP 文件头无效")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ValueError(f"ZIP 文件项超过安全上限（{MAX_ARCHIVE_MEMBERS}）")
            total = sum(max(0, int(item.file_size or 0)) for item in infos)
            if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError("ZIP 解压后超过安全上限")
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ValueError("ZIP 内容损坏或无法解析") from exc


def _zip_member_is_safe(name: str) -> bool:
    """Reject absolute/traversal paths even though ZIPs are never extracted."""
    normalized = str(name or "").replace("\\", "/")
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return False
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    return bool(parts) and all(part != ".." for part in parts)


def _looks_binary(raw: bytes) -> bool:
    if not raw:
        return False
    sample = raw[:8192]
    if b"\x00" in sample:
        return True
    control = sum(1 for byte in sample if byte < 9 or (13 < byte < 32))
    return control / max(1, len(sample)) > 0.05


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_display_name(name: str) -> str:
    name = Path(name or "attachment").name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:180] or "attachment"


def _decode_text(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8-replace"


def _natural_key(value: str) -> list[Any]:
    return [int(piece) if piece.isdigit() else piece.lower() for piece in re.split(r"(\d+)", value)]


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg"}:
            self._skip += 1
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg"} and self._skip:
            self._skip -= 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip() + " ")

    def text(self) -> str:
        value = "".join(self.parts)
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


class AttachmentService:
    def __init__(self) -> None:
        self._meta_lock = threading.RLock()

    def reconcile(self) -> dict[str, int]:
        """Quarantine incomplete/orphaned writes without destroying user data."""
        quarantine = UPLOAD_DIR / "orphaned"
        quarantine.mkdir(parents=True, exist_ok=True)
        moved = 0
        removed_temps = 0

        for path in list(UPLOAD_DIR.iterdir()):
            if not path.is_file():
                continue
            if path.name.startswith(".") and path.name.endswith(".tmp"):
                path.unlink(missing_ok=True)
                removed_temps += 1

        metas: dict[str, dict[str, Any]] = {}
        for meta_path in UPLOAD_DIR.glob("*.json"):
            attachment_id = meta_path.stem
            meta = self._load_meta(meta_path)
            stored_name = str((meta or {}).get("stored_name") or "")
            valid = (
                re.fullmatch(r"[a-f0-9]{32}", attachment_id)
                and stored_name
                and Path(stored_name).name == stored_name
                and (UPLOAD_DIR / stored_name).is_file()
            )
            if not valid:
                destination = quarantine / f"{meta_path.name}.{uuid.uuid4().hex[:8]}"
                os.replace(meta_path, destination)
                moved += 1
                continue
            metas[stored_name] = meta or {}

        candidate_re = re.compile(r"^[a-f0-9]{32}\.[A-Za-z0-9]+$")
        for path in list(UPLOAD_DIR.iterdir()):
            if not path.is_file() or path.suffix == ".json":
                continue
            if candidate_re.fullmatch(path.name) and path.name not in metas:
                destination = quarantine / f"{path.name}.{uuid.uuid4().hex[:8]}"
                os.replace(path, destination)
                moved += 1
        return {"quarantined": moved, "removed_temps": removed_temps}

    def _meta_path(self, attachment_id: str) -> Path:
        return UPLOAD_DIR / f"{attachment_id}.json"

    def _load_meta(self, path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text("utf-8"))
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def get(self, attachment_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[a-f0-9]{32}", str(attachment_id or "")):
            return None
        meta = self._load_meta(self._meta_path(attachment_id))
        if not meta:
            return None
        file_path = UPLOAD_DIR / meta.get("stored_name", "")
        if not file_path.is_file():
            return None
        return self.public_meta(meta)

    def get_internal(self, attachment_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[a-f0-9]{32}", str(attachment_id or "")):
            return None
        raw_meta = self._load_meta(self._meta_path(attachment_id))
        if not raw_meta:
            return None
        path = UPLOAD_DIR / str(raw_meta.get("stored_name") or "")
        if not path.is_file():
            return None
        # Keep private analysis paths for the model adapter, while retaining the
        # same normalized public fields used by the rest of the application.
        meta = {**raw_meta, **self.public_meta(raw_meta)}
        meta["path"] = str(path)
        return meta

    def public_meta(self, meta: dict[str, Any]) -> dict[str, Any]:
        attachment_id = meta.get("id", "")
        public = {
            "id": attachment_id,
            "name": meta.get("name", "attachment"),
            "stored_name": meta.get("stored_name", ""),
            "size": int(meta.get("size", 0) or 0),
            "mime_type": meta.get("mime_type", "application/octet-stream"),
            "kind": meta.get("kind", "file"),
            "status": meta.get("status", "ready"),
            "parse_message": meta.get("parse_message", ""),
            "extracted_chars": int(meta.get("extracted_chars", 0) or 0),
            "truncated": bool(meta.get("truncated", False)),
            "page_count": meta.get("page_count"),
            "encoding": meta.get("encoding"),
            "created_at": meta.get("created_at"),
            "preview_url": f"/api/attachments/{attachment_id}/content",
        }
        if meta.get("kind") == "video":
            public.update({
                "video_duration": float(meta.get("video_duration") or 0.0),
                "video_width": int(meta.get("video_width") or 0),
                "video_height": int(meta.get("video_height") or 0),
                "video_frame_count": int(meta.get("video_frame_count") or 0),
                "video_has_audio": bool(meta.get("video_has_audio")),
            })
        if meta.get("kind") == "archive":
            public.update({
                "archive_member_count": int(meta.get("archive_member_count") or 0),
                "archive_text_file_count": int(meta.get("archive_text_file_count") or 0),
                "archive_skipped_count": int(meta.get("archive_skipped_count") or 0),
            })
        if meta.get("kind") == "audio":
            analysis_status = str(meta.get("analysis_status") or "waiting_install")
            public.update({
                "analysis_engine": "ocean-listen",
                "analysis_status": analysis_status,
                "analysis_stage": str(meta.get("analysis_stage") or "等待本机听觉"),
                "analysis_progress": max(0, min(100, int(meta.get("analysis_progress") or 0))),
                "analysis_error": str(meta.get("analysis_error") or "")[:1000],
                "analysis_started_at": meta.get("analysis_started_at"),
                "analysis_finished_at": meta.get("analysis_finished_at"),
                "analysis_summary_chars": int(meta.get("extracted_chars") or 0),
                "analysis_local_only": True,
                "report_url": (
                    f"/api/audio-analysis/{attachment_id}/report"
                    if analysis_status == "ready" and meta.get("analysis_report_rel") else ""
                ),
                "spectrogram_url": (
                    f"/api/audio-analysis/{attachment_id}/spectrogram"
                    if analysis_status == "ready" and meta.get("analysis_spectrogram_rel") else ""
                ),
            })
        return public

    def update_meta(self, attachment_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        """Atomically merge trusted service-owned attachment metadata."""
        if not re.fullmatch(r"[a-f0-9]{32}", str(attachment_id or "")):
            return None
        with self._meta_lock:
            meta_path = self._meta_path(attachment_id)
            meta = self._load_meta(meta_path)
            if not meta:
                return None
            data_path = UPLOAD_DIR / str(meta.get("stored_name") or "")
            if not data_path.is_file():
                return None
            next_meta = {**meta, **dict(patch or {})}
            temp = meta_path.with_name(f".{meta_path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temp.write_text(
                    json.dumps(next_meta, ensure_ascii=False, indent=2), "utf-8"
                )
                os.replace(temp, meta_path)
            finally:
                temp.unlink(missing_ok=True)
            return self.public_meta(next_meta)

    def file_path(self, attachment_id: str) -> Path | None:
        meta = self.get(attachment_id)
        if not meta:
            return None
        path = UPLOAD_DIR / meta["stored_name"]
        try:
            path.resolve().relative_to(UPLOAD_DIR.resolve())
        except ValueError:
            return None
        return path if path.is_file() else None

    def extracted_text(self, attachment_id: str) -> str:
        meta = self._load_meta(self._meta_path(attachment_id))
        if not meta:
            return ""
        return str(meta.get("extracted_text", "") or "")

    def save_bytes(self, filename: str, raw: bytes, content_type: str | None = None) -> dict[str, Any]:
        display_name = _safe_display_name(filename)
        suffix = Path(display_name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise ValueError(f"暂不支持 {suffix or '无扩展名'} 文件")
        if not raw:
            raise ValueError("文件内容为空")
        byte_limit = MAX_AUDIO_BYTES if suffix in AUDIO_EXTENSIONS else (MAX_VIDEO_BYTES if suffix in VIDEO_EXTENSIONS else MAX_UPLOAD_BYTES)
        if len(raw) > byte_limit:
            raise ValueError(f"文件超过 {byte_limit // 1024 // 1024} MB 上限")

        # Never trust multipart Content-Type. Validate executable inline formats
        # against their actual bytes and derive the response MIME from our own
        # extension allowlist.
        if suffix in IMAGE_EXTENSIONS:
            _validate_image(raw, suffix)
        elif suffix in AUDIO_EXTENSIONS:
            _validate_audio(raw, suffix)
        elif suffix in VIDEO_EXTENSIONS:
            _validate_video(raw, suffix)
        elif suffix == ".pdf":
            _validate_pdf(raw)
        elif suffix in ARCHIVE_EXTENSIONS:
            _validate_zip(raw)

        attachment_id = uuid.uuid4().hex
        stored_name = f"{attachment_id}{suffix}"
        path = UPLOAD_DIR / stored_name
        temp_path = UPLOAD_DIR / f".{stored_name}.{uuid.uuid4().hex}.tmp"
        meta_path = self._meta_path(attachment_id)
        meta_temp = meta_path.with_name(f".{meta_path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_bytes(raw)

        meta: dict[str, Any] = {
            "id": attachment_id,
            "name": display_name,
            "stored_name": stored_name,
            "size": len(raw),
            "mime_type": _safe_mime_for_suffix(suffix),
            "kind": "file",
            "status": "ready",
            "parse_message": "",
            "extracted_text": "",
            "extracted_chars": 0,
            "truncated": False,
            "created_at": _utc_now(),
        }

        try:
            if suffix in IMAGE_EXTENSIONS:
                meta.update({
                    "kind": "image",
                    "status": "ready",
                    "parse_message": "图片已验证并保存在本机；是否发送原图以本轮投递回执为准。",
                })
            elif suffix in AUDIO_EXTENSIONS:
                meta.update({
                    "kind": "audio",
                    "status": "analysis_waiting",
                    "parse_message": "音频已安全保存在本机，等待听海分析。",
                    "analysis_engine": "ocean-listen",
                    "analysis_status": "waiting_install",
                    "analysis_stage": "等待本机听觉",
                    "analysis_progress": 0,
                    "analysis_error": "",
                })
            elif suffix in VIDEO_EXTENSIONS:
                meta.update(self._extract_video(temp_path, attachment_id))
            elif suffix in ARCHIVE_EXTENSIONS:
                meta.update(self._extract_zip(temp_path))
            elif suffix == ".pdf":
                meta.update(self._extract_pdf(temp_path))
            elif suffix == ".docx":
                meta.update(self._extract_docx(temp_path))
            elif suffix == ".pptx":
                meta.update(self._extract_pptx(temp_path))
            elif suffix == ".xlsx":
                meta.update(self._extract_xlsx(temp_path))
            elif suffix == ".epub":
                meta.update(self._extract_epub(temp_path))
            else:
                meta.update(self._extract_text(raw))
        except Exception as exc:
            # Non-inline documents may still be useful as a download even when
            # text extraction fails. Images and PDFs were already strictly
            # validated above and never reach this fallback when malformed.
            meta.update({
                "status": "parse_error",
                "parse_message": f"解析失败，但文件已保存：{type(exc).__name__}",
                "extracted_text": "",
                "extracted_chars": 0,
            })

        try:
            meta_temp.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), "utf-8"
            )
            os.replace(temp_path, path)
            os.replace(meta_temp, meta_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            meta_temp.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise
        return self.public_meta(meta)

    async def save_upload(self, upload: Any) -> dict[str, Any]:
        # Reject at MAX+1 instead of reading an arbitrarily large request body
        # into RAM first. PDF/Office parsing is synchronous, so keep it off the
        # FastAPI event loop as well.
        suffix = Path(upload.filename or "attachment").suffix.lower()
        byte_limit = MAX_AUDIO_BYTES if suffix in AUDIO_EXTENSIONS else (MAX_VIDEO_BYTES if suffix in VIDEO_EXTENSIONS else MAX_UPLOAD_BYTES)
        raw = await upload.read(byte_limit + 1)
        if len(raw) > byte_limit:
            raise ValueError(
                f"文件超过 {byte_limit // 1024 // 1024} MB 上限"
            )
        return await asyncio.to_thread(
            self.save_bytes,
            upload.filename or "attachment",
            raw,
            upload.content_type,
        )

    def _extract_video(self, path: Path, attachment_id: str) -> dict[str, Any]:
        ffprobe = shutil.which("ffprobe")
        ffmpeg = shutil.which("ffmpeg")
        if not ffprobe or not ffmpeg:
            return {
                "kind": "video", "status": "parser_missing",
                "parse_message": "视频已保存，但缺少 ffmpeg/ffprobe，无法真正读取关键帧。",
                "extracted_text": "", "extracted_chars": 0, "video_frame_count": 0,
            }
        try:
            probe = subprocess.run([
                ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
            ], capture_output=True, text=True, timeout=15, check=True)
            info = json.loads(probe.stdout or "{}")
            streams = info.get("streams") if isinstance(info.get("streams"), list) else []
            video = next((x for x in streams if x.get("codec_type") == "video"), None)
            has_audio = any(x.get("codec_type") == "audio" for x in streams)
            if not video:
                if path.suffix.lower() == ".webm" and has_audio:
                    return {
                        "kind": "audio", "mime_type": "audio/webm", "status": "analysis_waiting",
                        "parse_message": "WebM 中检测到音频轨，已按音频保存，等待听海分析。",
                        "analysis_engine": "ocean-listen", "analysis_status": "waiting_install",
                        "analysis_stage": "等待本机听觉", "analysis_progress": 0, "analysis_error": "",
                        "extracted_text": "", "extracted_chars": 0,
                    }
                raise ValueError("文件里没有视频轨")
            duration = float((info.get("format") or {}).get("duration") or video.get("duration") or 0.0)
            width, height = int(video.get("width") or 0), int(video.get("height") or 0)
            frame_dir = UPLOAD_DIR / f"{attachment_id}.frames"
            shutil.rmtree(frame_dir, ignore_errors=True)
            frame_dir.mkdir(parents=True, exist_ok=False)
            # Keep the visual payload bounded: at most 8 evenly-spaced-ish frames, max 960px wide.
            interval = max(0.5, duration / 8.0) if duration > 0 else 2.0
            output = frame_dir / "frame-%02d.jpg"
            subprocess.run([
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
                "-vf", f"fps=1/{interval:.3f},scale='min(960,iw)':-2", "-frames:v", "8",
                "-q:v", "4", str(output)
            ], capture_output=True, timeout=35, check=True)
            frames = sorted(frame_dir.glob("frame-*.jpg"))[:8]
            if not frames:
                shutil.rmtree(frame_dir, ignore_errors=True)
                raise ValueError("没有抽取到可读关键帧")
            rels = [str(x.relative_to(UPLOAD_DIR)) for x in frames]
            summary = (
                f"视频时长约 {duration:.1f} 秒；分辨率 {width}×{height}；"
                f"已在本机抽取 {len(frames)} 张关键帧；音轨={'有' if has_audio else '无'}。"
            )
            return {
                "kind": "video", "status": "parsed", "parse_message": "视频已读取关键帧，可发送给视觉模型。",
                "extracted_text": summary, "extracted_chars": len(summary), "truncated": False,
                "video_duration": duration, "video_width": width, "video_height": height,
                "video_has_audio": has_audio, "video_frame_count": len(frames), "video_frame_rels": rels,
            }
        except Exception as exc:
            shutil.rmtree(UPLOAD_DIR / f"{attachment_id}.frames", ignore_errors=True)
            return {
                "kind": "video", "status": "parse_error",
                "parse_message": f"视频读取失败：{type(exc).__name__}: {str(exc)[:160]}",
                "extracted_text": "", "extracted_chars": 0, "video_frame_count": 0,
            }

    def _video_frame_data_urls(self, item: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        for rel in item.get("video_frame_rels") or []:
            path = (UPLOAD_DIR / str(rel)).resolve()
            try:
                path.relative_to(UPLOAD_DIR.resolve())
            except ValueError:
                continue
            if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg"}:
                continue
            if path.stat().st_size > MAX_NATIVE_BYTES:
                continue
            urls.append("data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii"))
            if len(urls) >= 8:
                break
        return urls

    def _extract_text(self, raw: bytes) -> dict[str, Any]:
        text, encoding = _decode_text(raw)
        truncated = len(text) > MAX_EXTRACT_CHARS
        text = text[:MAX_EXTRACT_CHARS]
        return {
            "kind": "text",
            "status": "parsed",
            "parse_message": "文字已提取" + ("（已截断）" if truncated else ""),
            "extracted_text": text,
            "extracted_chars": len(text),
            "truncated": truncated,
            "encoding": encoding,
        }

    def _extract_pdf(self, path: Path) -> dict[str, Any]:
        if PdfReader is None:
            return {
                "kind": "pdf",
                "status": "parser_missing",
                "parse_message": "缺少 pypdf，文件已保存但暂未提取文字。",
                "extracted_text": "",
                "extracted_chars": 0,
            }
        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        parts: list[str] = []
        total = 0
        parsed_pages = 0
        for index, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            if page_text.strip():
                chunk = f"\n\n--- 第 {index} 页 ---\n{page_text.strip()}"
                remaining = MAX_EXTRACT_CHARS - total
                if remaining <= 0:
                    break
                parts.append(chunk[:remaining])
                total += min(len(chunk), remaining)
                parsed_pages += 1
        text = "".join(parts).strip()
        truncated = page_count > MAX_PDF_PAGES or total >= MAX_EXTRACT_CHARS
        status = "parsed" if text else "no_text"
        message = "PDF 文字已提取" if text else "PDF 没有可提取文字，可能是扫描件；原生 GPT/Claude 仍可读取文件。"
        if truncated:
            message += "（已截断）"
        return {
            "kind": "pdf",
            "status": status,
            "parse_message": message,
            "extracted_text": text,
            "extracted_chars": len(text),
            "truncated": truncated,
            "page_count": page_count,
            "parsed_pages": parsed_pages,
        }

    def _open_archive(self, path: Path) -> zipfile.ZipFile:
        archive = zipfile.ZipFile(path)
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            archive.close()
            raise ValueError(f"压缩文档文件项超过安全上限（{MAX_ARCHIVE_MEMBERS}）")
        total = sum(max(0, int(item.file_size or 0)) for item in infos)
        if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            archive.close()
            raise ValueError("压缩文档解压后超过安全上限")
        return archive

    @staticmethod
    def _xml_text(raw: bytes, text_tag: str) -> str:
        root = ET.fromstring(raw)
        parts: list[str] = []
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] == text_tag and node.text:
                parts.append(node.text)
        return " ".join(part.strip() for part in parts if part.strip())

    def _finish_archive_text(self, text: str, *, kind: str, label: str, units: int | None = None) -> dict[str, Any]:
        text = text.strip()
        truncated = len(text) > MAX_EXTRACT_CHARS
        text = text[:MAX_EXTRACT_CHARS]
        return {
            "kind": kind,
            "status": "parsed" if text else "no_text",
            "parse_message": (
                f"{label}文字已在本机提取" if text else f"{label}中没有找到可提取文字"
            ) + ("（已截断）" if truncated else ""),
            "extracted_text": text,
            "extracted_chars": len(text),
            "truncated": truncated,
            "unit_count": units,
        }

    def _extract_zip(self, path: Path) -> dict[str, Any]:
        """Read a generic ZIP locally and expose only bounded text + a file manifest.

        Nothing is extracted or executed.  Unsafe paths, symlinks, encrypted members,
        oversized members and binary payloads remain opaque and are only represented
        by filename/size in the manifest.
        """
        parts: list[str] = []
        text_files = 0
        skipped = 0
        listed = 0
        text_basenames = {
            "dockerfile", "makefile", "license", "readme", "changelog",
            "gemfile", "procfile", ".gitignore", ".dockerignore",
            ".env.example", ".editorconfig",
        }
        with self._open_archive(path) as archive:
            infos = archive.infolist()
            manifest: list[str] = []
            safe_infos: list[zipfile.ZipInfo] = []
            for info in infos:
                raw_name = str(info.filename or "")
                is_dir = info.is_dir() or raw_name.endswith("/")
                safe = _zip_member_is_safe(raw_name)
                unix_mode = (int(info.external_attr or 0) >> 16) & 0xFFFF
                is_symlink = (unix_mode & 0o170000) == 0o120000
                flags: list[str] = []
                if not safe:
                    flags.append("unsafe-path")
                if is_symlink:
                    flags.append("symlink")
                if info.flag_bits & 0x1:
                    flags.append("encrypted")
                label = "DIR " if is_dir else "FILE"
                suffix = f" [{' '.join(flags)}]" if flags else ""
                if listed < MAX_ARCHIVE_LISTED_MEMBERS:
                    display_member = raw_name if len(raw_name) <= 320 else raw_name[:317] + "..."
                    line = f"{label} {display_member} ({int(info.file_size or 0)} bytes){suffix}"
                    manifest_chars = sum(len(row) + 1 for row in manifest)
                    if manifest_chars + len(line) <= MAX_ARCHIVE_MANIFEST_CHARS:
                        manifest.append(line)
                        listed += 1
                if is_dir or not safe or is_symlink or (info.flag_bits & 0x1):
                    if not is_dir:
                        skipped += 1
                    continue
                member_suffix = Path(raw_name).suffix.lower()
                member_basename = Path(raw_name).name.lower()
                if member_suffix in TEXT_EXTENSIONS or member_basename in text_basenames:
                    safe_infos.append(info)
                else:
                    skipped += 1

            if len(infos) > listed:
                manifest.append(f"... 另有 {len(infos) - listed} 个文件项未在目录清单中展开")
            parts.append("--- ZIP 目录清单 ---\n" + "\n".join(manifest))

            # Read only the allowlisted textual members selected above.
            for info in safe_infos:
                if len("\n\n".join(parts)) >= MAX_EXTRACT_CHARS:
                    break
                name = str(info.filename or "")
                if int(info.file_size or 0) > MAX_ARCHIVE_MEMBER_READ_BYTES:
                    parts.append(
                        f"--- ZIP 文本文件：{name} ---\n"
                        f"[跳过正文：单文件超过 {MAX_ARCHIVE_MEMBER_READ_BYTES // 1024 // 1024} MB 读取上限]"
                    )
                    skipped += 1
                    continue
                try:
                    with archive.open(info, "r") as member:
                        raw = member.read(MAX_ARCHIVE_MEMBER_READ_BYTES + 1)
                except (RuntimeError, NotImplementedError, zipfile.BadZipFile):
                    skipped += 1
                    continue
                if len(raw) > MAX_ARCHIVE_MEMBER_READ_BYTES or _looks_binary(raw):
                    skipped += 1
                    continue
                text, encoding = _decode_text(raw)
                remaining = MAX_EXTRACT_CHARS - len("\n\n".join(parts))
                if remaining <= 0:
                    break
                header = f"--- ZIP 文本文件：{name}；编码={encoding} ---\n"
                body_budget = max(0, min(
                    len(text),
                    MAX_ARCHIVE_MEMBER_TEXT_CHARS,
                    remaining - len(header),
                ))
                if body_budget <= 0:
                    break
                body = text[:body_budget]
                if len(text) > body_budget:
                    body += f"\n[该文件正文已截取前 {body_budget} 字符]"
                parts.append(header + body)
                text_files += 1

        joined = "\n\n".join(parts).strip()
        truncated = len(joined) > MAX_EXTRACT_CHARS
        joined = joined[:MAX_EXTRACT_CHARS]
        status = "parsed" if joined else "no_text"
        message = (
            f"ZIP 已在本机安全读取：{len(infos)} 个文件项，提取 {text_files} 个文本/代码文件；"
            "原 ZIP 不会发送给模型或中转站。"
        )
        if skipped:
            message += f" 跳过 {skipped} 个二进制/不安全/超限文件项。"
        if truncated:
            message += "（文字已截断）"
        return {
            "kind": "archive",
            "status": status,
            "parse_message": message,
            "extracted_text": joined,
            "extracted_chars": len(joined),
            "truncated": truncated,
            "archive_member_count": len(infos),
            "archive_text_file_count": text_files,
            "archive_skipped_count": skipped,
        }

    def _extract_docx(self, path: Path) -> dict[str, Any]:
        with self._open_archive(path) as archive:
            raw = archive.read("word/document.xml")
        root = ET.fromstring(raw)
        paragraphs: list[str] = []
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] != "p":
                continue
            text = "".join(
                child.text or "" for child in node.iter()
                if child.tag.rsplit("}", 1)[-1] == "t"
            ).strip()
            if text:
                paragraphs.append(text)
        return self._finish_archive_text(
            "\n\n".join(paragraphs), kind="document", label="DOCX", units=len(paragraphs)
        )

    def _extract_pptx(self, path: Path) -> dict[str, Any]:
        parts: list[str] = []
        with self._open_archive(path) as archive:
            names = sorted(
                (
                    name for name in archive.namelist()
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                ),
                key=_natural_key,
            )
            for index, name in enumerate(names, start=1):
                text = self._xml_text(archive.read(name), "t")
                if text:
                    parts.append(f"--- 第 {index} 张幻灯片 ---\n{text}")
        return self._finish_archive_text(
            "\n\n".join(parts), kind="document", label="PPTX", units=len(names)
        )

    def _extract_xlsx(self, path: Path) -> dict[str, Any]:
        parts: list[str] = []
        with self._open_archive(path) as archive:
            names = set(archive.namelist())
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in root:
                    shared.append("".join(
                        node.text or "" for node in item.iter()
                        if node.tag.rsplit("}", 1)[-1] == "t"
                    ))
            sheets = sorted(
                (name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
                key=_natural_key,
            )
            for sheet_index, name in enumerate(sheets, start=1):
                root = ET.fromstring(archive.read(name))
                rows: list[str] = []
                for row in root.iter():
                    if row.tag.rsplit("}", 1)[-1] != "row":
                        continue
                    cells: list[str] = []
                    for cell in row:
                        if cell.tag.rsplit("}", 1)[-1] != "c":
                            continue
                        ref = cell.attrib.get("r", "?")
                        cell_type = cell.attrib.get("t", "")
                        value = ""
                        if cell_type == "inlineStr":
                            value = "".join(
                                node.text or "" for node in cell.iter()
                                if node.tag.rsplit("}", 1)[-1] == "t"
                            )
                        else:
                            raw_value = next((
                                node.text or "" for node in cell
                                if node.tag.rsplit("}", 1)[-1] == "v"
                            ), "")
                            if cell_type == "s" and raw_value.isdigit():
                                index = int(raw_value)
                                value = shared[index] if 0 <= index < len(shared) else raw_value
                            else:
                                value = raw_value
                        formula = next((
                            node.text or "" for node in cell
                            if node.tag.rsplit("}", 1)[-1] == "f"
                        ), "")
                        if formula:
                            value = f"={formula}" + (f" → {value}" if value else "")
                        if value:
                            cells.append(f"{ref}={value}")
                    if cells:
                        rows.append("\t".join(cells))
                if rows:
                    parts.append(f"--- 工作表 {sheet_index} ---\n" + "\n".join(rows))
        return self._finish_archive_text(
            "\n\n".join(parts), kind="spreadsheet", label="XLSX", units=len(sheets)
        )

    def _extract_epub(self, path: Path) -> dict[str, Any]:
        parts: list[str] = []
        with self._open_archive(path) as archive:
            names = sorted(
                (
                    name for name in archive.namelist()
                    if Path(name).suffix.lower() in {".xhtml", ".html", ".htm"}
                    and not name.lower().endswith(("nav.xhtml", "toc.html", "toc.xhtml"))
                ),
                key=_natural_key,
            )
            for index, name in enumerate(names, start=1):
                raw = archive.read(name)
                html, _ = _decode_text(raw)
                parser = _HTMLTextExtractor()
                parser.feed(html)
                text = parser.text()
                if text:
                    parts.append(f"--- 章节 {index} ---\n{text}")
        return self._finish_archive_text(
            "\n\n".join(parts), kind="ebook", label="EPUB", units=len(names)
        )

    def delete(self, attachment_id: str) -> bool:
        """Delete through a private trash directory so partial moves can roll back."""
        meta_path = self._meta_path(attachment_id)
        meta = self._load_meta(meta_path)
        if not meta:
            return False
        stored_name = str(meta.get("stored_name") or "")
        data_path = UPLOAD_DIR / stored_name
        if not stored_name or data_path.parent != UPLOAD_DIR:
            return False
        trash_dir = UPLOAD_DIR / ".trash" / f"{attachment_id}-{uuid.uuid4().hex}"
        trash_data = trash_dir / data_path.name
        trash_meta = trash_dir / meta_path.name
        moved_data = False
        moved_meta = False
        try:
            trash_dir.mkdir(parents=True, exist_ok=False)
            if data_path.exists():
                os.replace(data_path, trash_data)
                moved_data = True
            os.replace(meta_path, trash_meta)
            moved_meta = True
        except Exception:
            try:
                if moved_meta and trash_meta.exists():
                    os.replace(trash_meta, meta_path)
                if moved_data and trash_data.exists():
                    os.replace(trash_data, data_path)
            finally:
                shutil.rmtree(trash_dir, ignore_errors=True)
            return False
        shutil.rmtree(trash_dir, ignore_errors=True)
        shutil.rmtree(UPLOAD_DIR / f"{attachment_id}.frames", ignore_errors=True)
        return True

    def resolve_many(self, attachment_ids: Iterable[str], limit: int = 8) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for attachment_id in attachment_ids:
            attachment_id = str(attachment_id or "")
            if attachment_id in seen:
                continue
            seen.add(attachment_id)
            item = self.get_internal(attachment_id)
            if item:
                result.append(item)
            if len(result) >= limit:
                break
        return result

    def model_text_context(self, items: list[dict[str, Any]], max_total_chars: int = 90000) -> str:
        items = [item for item in items if not item.get("_native_only")]
        if not items or int(max_total_chars or 0) <= 0:
            return ""
        parts = [
            "【当前资料】",
            "回答时请尽量用“文件名 + 页码/幻灯片/工作表/章节”指出依据；没有对应编号时不要捏造。",
        ]
        used = 0
        for index, item in enumerate(items, start=1):
            header = (
                f"资料{index}：{item['name']}；类型={item['kind']}；"
                f"大小={item['size']} bytes；解析状态={item['status']}。"
            )
            text = self.extracted_text(item["id"])
            if text:
                remaining = max_total_chars - used
                excerpt = text[:max(0, remaining)]
                parts.append(f"{header}\n<file_text name={json.dumps(item['name'], ensure_ascii=False)}>\n{excerpt}\n</file_text>")
                used += len(excerpt)
            else:
                parts.append(f"{header}\n{item.get('parse_message') or '暂无本地文字内容。'}")
            if used >= max_total_chars:
                parts.append("附件文字总量达到上下文上限，其余内容未加入本轮请求。")
                break
        return "\n\n".join(parts)

    def pinned_index_context(
        self,
        items: list[dict[str, Any]],
        query: str,
        *,
        max_total_chars: int = 6000,
    ) -> tuple[str, list[str]]:
        """Keep pinned files available as a short index + relevant snippets.

        Full extracted text remains on disk.  Selecting the file explicitly for
        the current turn still uses ``build_current_user_content`` and can send
        the larger one-turn body; pinned mode no longer repeats it every turn.
        """
        items = [item for item in items if not item.get("_native_only")]
        if not items or max_total_chars <= 0:
            return "", []
        budget = max(0, int(max_total_chars))
        parts = [
            "【资料工作室 · 常驻索引】",
            "这些资料一直可用，但本轮只提供短索引与当前问题相关片段；需要全文时请把对应文件作为本轮附件使用。",
        ]
        used = sum(len(part) for part in parts) + 2
        for index, item in enumerate(items, 1):
            line = (
                f"{index}. {item.get('name') or '资料'}；类型={item.get('kind') or 'file'}；"
                f"可用文字={max(0, int(item.get('extracted_chars') or 0))} 字。"
            )
            if used + len(line) + 2 > min(budget, 1800):
                break
            parts.append(line)
            used += len(line) + 2

        remaining = max(0, budget - used)
        excerpts, used_ids = self.relevant_text_context(
            items,
            query,
            max_total_chars=remaining,
            max_chunks_per_file=2,
        )
        if excerpts:
            parts.append(excerpts)
        return "\n\n".join(parts)[:budget], used_ids

    @staticmethod
    def wants_visual_refresh(query: str, item: dict[str, Any] | None = None) -> bool:
        """True only when the current user turn explicitly asks to inspect visuals."""
        text = str(query or "").strip().lower()
        if not text:
            return False
        name = str((item or {}).get("name") or "").strip().lower()
        visual_terms = (
            "图片", "照片", "截图", "画面", "图里", "图中", "这张图", "这幅图",
            "视频", "录像", "片段", "关键帧", "镜头", "画面里", "视频里",
            "image", "photo", "picture", "screenshot", "video", "frame", "visual",
        )
        if any(term in text for term in visual_terms):
            return True
        return bool(name and name in text)

    @staticmethod
    def _retrieval_terms(query: str) -> list[str]:
        """Small deterministic tokenizer for local file snippet selection.

        This deliberately does not call an embedding model or a paid API.  A
        handful of CJK n-grams plus Latin words works well enough to avoid
        attaching an entire book when the user asks about one topic.
        """
        text = str(query or "").strip().lower()
        terms: list[str] = []
        for segment in re.findall(r"[\u3400-\u9fff]{2,}", text):
            if 2 <= len(segment) <= 10:
                terms.append(segment)
            for width in (4, 3, 2):
                if len(segment) >= width:
                    terms.extend(
                        segment[index:index + width]
                        for index in range(len(segment) - width + 1)
                    )
        terms.extend(re.findall(r"[a-z0-9_\-]{3,}", text))
        ignored = {"这个", "那个", "什么", "怎么", "可以", "我们", "你们", "文件", "资料", "看看"}
        unique: list[str] = []
        for term in terms:
            if term in ignored or term in unique:
                continue
            unique.append(term)
            if len(unique) >= 18:
                break
        return unique

    @staticmethod
    def _text_chunks(text: str, target: int = 900) -> list[str]:
        raw_parts = [part.strip() for part in re.split(r"\n{2,}", text or "") if part.strip()]
        chunks: list[str] = []
        buffer = ""
        for part in raw_parts:
            if len(part) > target * 2:
                if buffer:
                    chunks.append(buffer)
                    buffer = ""
                chunks.extend(part[pos:pos + target] for pos in range(0, len(part), target))
                continue
            candidate = f"{buffer}\n\n{part}".strip() if buffer else part
            if len(candidate) > target and buffer:
                chunks.append(buffer)
                buffer = part
            else:
                buffer = candidate
        if buffer:
            chunks.append(buffer)
        return chunks

    def relevant_text_context(
        self,
        items: list[dict[str, Any]],
        query: str,
        *,
        max_total_chars: int = 10000,
        max_chunks_per_file: int = 3,
    ) -> tuple[str, list[str]]:
        """Return query-matched excerpts and the IDs that were actually used."""
        terms = self._retrieval_terms(query)
        if not items or not terms or max_total_chars <= 0:
            return "", []
        candidates: list[tuple[float, str, str, str]] = []
        for item in items:
            attachment_id = str(item.get("id") or "")
            text = self.extracted_text(attachment_id)
            if not text:
                continue
            name = str(item.get("name") or "资料")
            name_lower = name.lower()
            for index, chunk in enumerate(self._text_chunks(text)):
                lower = chunk.lower()
                matched = [term for term in terms if term in lower]
                if not matched:
                    continue
                score = sum(1.0 + min(4, lower.count(term)) * .35 for term in matched)
                score += sum(1.25 for term in terms if term in name_lower)
                # Keep a light recency/ordering preference when scores tie.
                score -= index * .0001
                candidates.append((score, attachment_id, name, chunk))
        candidates.sort(key=lambda entry: entry[0], reverse=True)

        per_file: dict[str, int] = {}
        selected: list[tuple[str, str, str]] = []
        used_chars = 0
        for _score, attachment_id, name, chunk in candidates:
            if per_file.get(attachment_id, 0) >= max_chunks_per_file:
                continue
            remaining = max_total_chars - used_chars
            if remaining < 160:
                break
            excerpt = chunk[:remaining]
            selected.append((attachment_id, name, excerpt))
            per_file[attachment_id] = per_file.get(attachment_id, 0) + 1
            used_chars += len(excerpt)
        if not selected:
            return "", []
        parts = [
            "【资料工作室 · 按需摘取】",
            "以下片段由本机按当前问题匹配；只据片段作答，未出现的内容不要猜测。",
        ]
        used_ids: list[str] = []
        for attachment_id, name, excerpt in selected:
            if attachment_id not in used_ids:
                used_ids.append(attachment_id)
            parts.append(
                f"<file_excerpt name={json.dumps(name, ensure_ascii=False)}>\n"
                f"{excerpt}\n</file_excerpt>"
            )
        return "\n\n".join(parts), used_ids

    def _zip_query_context(
        self,
        item: dict[str, Any],
        query: str,
        *,
        max_chars: int = 12000,
        max_files: int = 4,
    ) -> str:
        """Read query-relevant text members directly from a local ZIP.

        This lets a user ask about a file that appears late in a large project
        archive without forwarding the ZIP itself or preloading the whole
        archive into every prompt.
        """
        if item.get("kind") != "archive" or max_chars <= 0:
            return ""
        terms = self._retrieval_terms(query)
        if not terms:
            return ""
        path = Path(str(item.get("path") or ""))
        if not path.is_file():
            return ""

        candidates: list[tuple[float, zipfile.ZipInfo, str | None]] = []
        try:
            with self._open_archive(path) as archive:
                textual: list[zipfile.ZipInfo] = []
                for info in archive.infolist():
                    name = str(info.filename or "")
                    if info.is_dir() or not _zip_member_is_safe(name):
                        continue
                    unix_mode = (int(info.external_attr or 0) >> 16) & 0xFFFF
                    if (unix_mode & 0o170000) == 0o120000 or (info.flag_bits & 0x1):
                        continue
                    suffix = Path(name).suffix.lower()
                    basename = Path(name).name.lower()
                    if suffix not in TEXT_EXTENSIONS and basename not in {
                        "dockerfile", "makefile", "license", "readme", "changelog",
                        "gemfile", "procfile", ".gitignore", ".dockerignore",
                        ".env.example", ".editorconfig",
                    }:
                        continue
                    textual.append(info)
                    lower_name = name.lower()
                    name_hits = [term for term in terms if term in lower_name]
                    if name_hits:
                        score = 100.0 * len(name_hits) + sum(lower_name.count(term) for term in name_hits)
                        candidates.append((score, info, None))

                # If filenames alone do not fully answer the query, scan a
                # bounded prefix of source files locally for content matches.
                seen_names = {str(entry[1].filename) for entry in candidates}
                for info in textual[:240]:
                    if str(info.filename) in seen_names:
                        continue
                    if int(info.file_size or 0) > MAX_ARCHIVE_MEMBER_READ_BYTES:
                        continue
                    try:
                        with archive.open(info, "r") as member:
                            raw = member.read(min(MAX_ARCHIVE_MEMBER_READ_BYTES, 65536) + 1)
                    except Exception:
                        continue
                    if _looks_binary(raw):
                        continue
                    sample, _encoding = _decode_text(raw[:65536])
                    lower = sample.lower()
                    matched = [term for term in terms if term in lower]
                    if not matched:
                        continue
                    score = sum(1.5 + min(5, lower.count(term)) * .4 for term in matched)
                    candidates.append((score, info, sample))

                if not candidates:
                    return ""
                candidates.sort(key=lambda row: row[0], reverse=True)
                parts = ["【ZIP · 按当前问题本机读取】"]
                used_names: set[str] = set()
                used = len(parts[0])
                selected = 0
                for _score, info, cached_sample in candidates:
                    name = str(info.filename or "")
                    if name in used_names:
                        continue
                    remaining = max_chars - used
                    if remaining < 200 or selected >= max_files:
                        break
                    try:
                        if cached_sample is None:
                            with archive.open(info, "r") as member:
                                raw = member.read(MAX_ARCHIVE_MEMBER_READ_BYTES + 1)
                            if len(raw) > MAX_ARCHIVE_MEMBER_READ_BYTES or _looks_binary(raw):
                                continue
                            text, encoding = _decode_text(raw)
                        else:
                            # Re-read a filename hit in full; content-scan hits
                            # can use their cached prefix unless more budget is available.
                            with archive.open(info, "r") as member:
                                raw = member.read(MAX_ARCHIVE_MEMBER_READ_BYTES + 1)
                            if len(raw) > MAX_ARCHIVE_MEMBER_READ_BYTES or _looks_binary(raw):
                                continue
                            text, encoding = _decode_text(raw)
                    except Exception:
                        continue
                    header = f"--- ZIP 文件：{name}；编码={encoding} ---\n"
                    body_budget = min(MAX_ARCHIVE_MEMBER_TEXT_CHARS, max(0, remaining - len(header)))
                    if body_budget <= 0:
                        break
                    body = text[:body_budget]
                    if len(text) > body_budget:
                        body += f"\n[该文件正文已截取前 {body_budget} 字符]"
                    chunk = header + body
                    parts.append(chunk)
                    used += len(chunk) + 2
                    used_names.add(name)
                    selected += 1
                return "\n\n".join(parts) if selected else ""
        except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
            return ""

    @staticmethod
    def message_metadata(items: list[dict[str, Any]], *, display_text: str = "") -> dict[str, Any]:
        public_items = []
        for item in items:
            public_items.append({key: item.get(key) for key in (
                "id", "name", "size", "mime_type", "kind", "status", "parse_message",
                "extracted_chars", "truncated", "page_count", "created_at", "preview_url",
                "analysis_engine", "analysis_status", "analysis_stage", "analysis_progress",
                "analysis_error", "analysis_summary_chars", "analysis_local_only",
                "report_url", "spectrogram_url",
                "video_duration", "video_width", "video_height", "video_frame_count", "video_has_audio",
                "archive_member_count", "archive_text_file_count", "archive_skipped_count",
            )})
        return {
            "message_type": "mixed" if display_text and public_items else ("file" if public_items else "text"),
            "display_text": display_text,
            "attachments": public_items,
        }

    def _data_url(self, item: dict[str, Any]) -> str | None:
        path = Path(item["path"])
        if not path.is_file() or path.stat().st_size > MAX_NATIVE_BYTES:
            return None
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{item['mime_type']};base64,{encoded}"

    def _analysis_image_data_url(self, item: dict[str, Any]) -> str | None:
        relative = str(item.get("analysis_spectrogram_rel") or "").strip()
        if not relative:
            return None
        path = (DATA_DIR / relative).resolve()
        try:
            path.relative_to(DATA_DIR.resolve())
        except ValueError:
            return None
        if not path.is_file() or path.suffix.lower() != ".png":
            return None
        if path.stat().st_size > MAX_NATIVE_BYTES:
            return None
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def build_current_user_content(
        self,
        text: str,
        items: list[dict[str, Any]],
        protocol: str,
        *,
        max_local_chars: int = 90000,
        extra_context: str = "",
    ) -> str | list[dict[str, Any]]:
        """构造当前用户消息。

        兼容协议只接收纯文字；原生 GPT/Claude 尽量附带二进制内容块。
        PDF 同时保留本地提取文本，避免原生文件解析不可用时完全失明。
        """
        text = text.strip() or "请查看我发送的附件。"
        max_local_chars = max(0, int(max_local_chars or 0))
        zip_focus_budget = min(14000, max(0, max_local_chars // 2))
        zip_focus_parts: list[str] = []
        zip_focus_used = 0
        for item in items:
            if item.get("kind") != "archive" or zip_focus_used >= zip_focus_budget:
                continue
            focus = self._zip_query_context(
                item, text, max_chars=zip_focus_budget - zip_focus_used
            )
            if focus:
                zip_focus_parts.append(focus)
                zip_focus_used += len(focus)
        base_budget = max(0, max_local_chars - zip_focus_used)
        local_context = self.model_text_context(items, max_total_chars=base_budget)
        if zip_focus_parts:
            local_context = f"{local_context}\n\n" + "\n\n".join(zip_focus_parts)
        if extra_context:
            local_context = f"{local_context}\n\n{extra_context}".strip()
        if protocol == "claude_code_p":
            # The supplied P-mode reference validates stream-json image blocks.
            # Keep documents text-first: their extracted text is already present
            # in local_context, while native PDF/file blocks are not part of the
            # P-mode tutorial's stable input contract.
            blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
            for item in items:
                if item["kind"] == "video":
                    for frame in self._video_frame_data_urls(item):
                        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame.split(",", 1)[1]}})
                    continue
                if item["kind"] == "audio":
                    spectrogram = self._analysis_image_data_url(item)
                    if spectrogram:
                        blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": spectrogram.split(",", 1)[1],
                            },
                        })
                    continue
                if item["kind"] != "image":
                    continue
                data_url = self._data_url(item)
                if not data_url:
                    continue
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": item["mime_type"],
                        "data": data_url.split(",", 1)[1],
                    },
                })
            if local_context:
                blocks.append({"type": "text", "text": local_context})
            return blocks

        if protocol not in {"openai_responses", "anthropic"}:
            blind_images = [item.get("name") for item in items if item.get("kind") in {"image", "video"}]
            warning = ""
            if blind_images:
                warning = (
                    "【视觉投递说明】当前文字兼容通道没有收到这些原图/视频关键帧："
                    + "、".join(str(name or "图片") for name in blind_images)
                    + "。不要声称看见了图中内容；请提示使用视觉通道。"
                )
            return f"{text}\n\n{local_context}\n\n{warning}".strip()

        if protocol == "openai_responses":
            blocks: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
            for item in items:
                if item["kind"] == "video":
                    for frame in self._video_frame_data_urls(item):
                        blocks.append({"type": "input_image", "image_url": frame})
                    continue
                if item["kind"] == "audio":
                    spectrogram = self._analysis_image_data_url(item)
                    if spectrogram:
                        blocks.append({"type": "input_image", "image_url": spectrogram})
                    continue
                if item["kind"] == "image":
                    data_url = self._data_url(item)
                    if data_url:
                        blocks.append({"type": "input_image", "image_url": data_url})
                    continue
                if Path(item["name"]).suffix.lower() in OPENAI_NATIVE_FILE_EXTENSIONS:
                    data_url = self._data_url(item)
                    if data_url:
                        blocks.append({
                            "type": "input_file",
                            "file_data": data_url,
                            "filename": item["name"],
                        })
            if local_context:
                blocks.append({"type": "input_text", "text": local_context})
            return blocks

        blocks = [{"type": "text", "text": text}]
        for item in items:
            if item["kind"] == "video":
                for frame in self._video_frame_data_urls(item):
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame.split(",", 1)[1]}})
                continue
            if item["kind"] == "audio":
                spectrogram = self._analysis_image_data_url(item)
                if spectrogram:
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": spectrogram.split(",", 1)[1],
                        },
                    })
                continue
            if item["kind"] not in {"image", "pdf"}:
                continue
            data_url = self._data_url(item)
            if not data_url:
                continue
            encoded = data_url.split(",", 1)[1]
            if item["kind"] == "image":
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": item["mime_type"],
                        "data": encoded,
                    },
                })
            elif item["kind"] == "pdf":
                blocks.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": encoded,
                    },
                    "title": item["name"],
                })
        if local_context:
            blocks.append({"type": "text", "text": local_context})
        return blocks

    def history_text(self, content: str, metadata: dict[str, Any] | None) -> str:
        metadata = metadata or {}
        items = metadata.get("attachments") if isinstance(metadata.get("attachments"), list) else []
        sticker = metadata.get("sticker") if isinstance(metadata.get("sticker"), dict) else None
        parts = [content] if content else []
        for item in items:
            parts.append(f"[附件：{item.get('name', '文件')}，类型：{item.get('kind', 'file')}]" )
        if sticker:
            parts.append(f"[发送了表情包：{sticker.get('name') or sticker.get('id') or '表情包'}]")
        web_pages = metadata.get("web_pages") if isinstance(metadata.get("web_pages"), list) else []
        web_snapshot = webpage_service.history_context(web_pages)
        if web_snapshot:
            parts.append(web_snapshot)
        return "\n".join(part for part in parts if part).strip()


attachment_service = AttachmentService()
