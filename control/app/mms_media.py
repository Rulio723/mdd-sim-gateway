"""File naming helpers shared by MMS storage and downloads."""
from __future__ import annotations

import os
import re
import unicodedata


_EXTENSIONS = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/pjpeg": "jpg",
    "image/gif": "gif", "image/png": "png", "image/x-png": "png",
    "image/webp": "webp", "image/bmp": "bmp", "image/x-ms-bmp": "bmp",
    "image/heic": "heic", "image/heif": "heif", "image/avif": "avif",
    "image/vnd.wap.wbmp": "wbmp", "audio/amr": "amr", "audio/amr-wb": "awb",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mpeg3": "mp3",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/m4a": "m4a",
    "audio/3gpp": "3ga", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/wave": "wav", "video/3gpp": "3gp", "video/mp4": "mp4",
    "video/quicktime": "mov", "text/plain": "txt", "text/x-vcard": "vcf",
    "text/vcard": "vcf", "text/directory": "vcf", "text/x-vcalendar": "vcs",
    "text/calendar": "ics",
}
MAX_NAME_BYTES = 120
_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


def _base_type(content_type: str) -> str:
    return str(content_type or "").split(";", 1)[0].strip().lower()


def file_extension(content_type: str) -> str:
    return _EXTENSIONS.get(_base_type(content_type), "bin")


def _extension_of(name: str) -> str:
    stem, dot, extension = str(name or "").rpartition(".")
    return extension.lower() if dot and stem and extension.isascii() \
        and extension.isalnum() and len(extension) <= 10 else ""


def _clip_utf8(text: str, limit: int) -> str:
    return text.encode("utf-8")[:max(0, limit)].decode("utf-8", errors="ignore")


def display_name(name: str, content_type: str = "", *, max_bytes: int = MAX_NAME_BYTES) -> str:
    """Return a safe, byte-bounded display/download name without losing its extension."""
    text = unicodedata.normalize("NFC", str(name or ""))
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    text = _UNSAFE.sub("_", text).strip().lstrip(".").strip()
    extension = _extension_of(text)
    if not text or text == f".{extension}":
        return f"attachment.{file_extension(content_type)}"
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    suffix = f".{extension}" if extension else ""
    stem = text[:-len(suffix)] if suffix else text
    return _clip_utf8(stem, max_bytes - len(suffix)).rstrip() + suffix


def storage_name(seq: int, content_type: str) -> str:
    """Return a fresh on-disk name that never derives from a sender-supplied name."""
    return f"{int(seq):02d}-{os.urandom(8).hex()}.{file_extension(content_type)}"
